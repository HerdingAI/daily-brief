"""Voice: two-host script turns -> one WAV, voiced by HeyGen over OpenRouter.

HeyGen is cloud and billed per character, with the cost read back per generation. Turns are validated
before any request so a bad script never leaves a half-rendered file or a partial bill. There is no
backup engine: a failure fails the episode, and the hourly builder tries again.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf

Synth = Callable[[str, str], np.ndarray]  # (text, voice) -> mono float32 audio


class VoiceError(RuntimeError):
    """The script can't be voiced, or synthesis produced no audio."""


class VoiceUnavailable(VoiceError):
    """The provider, not the episode, is at fault: still failing after the retries (429/5xx/timeout/unreachable) or
    refusing the account (401/402/403). Nothing is charged to the item and the run stops trying further ones."""


class VoiceNotConfigured(VoiceError):
    """No OpenRouter key: the environment, not the episode, is at fault, so nothing is charged to the item."""


class DailyCapReached(VoiceError):
    """Today's billed spend plus this episode's estimate would exceed voice.daily_cap_usd; nothing was requested."""


def char_count(turns: list[dict]) -> int:
    """Characters sent to a per-character-billed engine (the stripped turn text, exactly what is posted)."""
    return sum(len(str(t.get("text", "")).strip()) for t in turns)


# ---- HeyGen Voice via OpenRouter ---------------------------------------------------------------------

ACCOUNT_CODES = (401, 402, 403)  # key rejected / out of credit / forbidden: the account, not the episode
OPENROUTER_BASE = "https://openrouter.ai/api/v1"  # module constant so tests can point it at a local server


def heygen_poster(model: str, key: str, timeout_s: int, retries: int, retry_wait_s: float,
                  sleep: Callable[[float], None] = time.sleep) -> Callable[[dict], tuple[bytes, str | None]]:
    """POST one turn -> (raw pcm, x-generation-id). Transient trouble (URLError, 429, 5xx) is retried up to
    `retries` times, then raises VoiceUnavailable. 401/402/403 (bad key, out of credit) are VoiceUnavailable at once;
    any other 4xx (bad voice, bad request) is the episode's own VoiceError."""

    def once(body: dict) -> tuple[bytes, str | None]:
        req = urllib.request.Request(f"{OPENROUTER_BASE}/audio/speech", data=json.dumps({"model": model} | body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return r.read(), r.headers.get("x-generation-id")

    def post(body: dict) -> tuple[bytes, str | None]:
        for attempt in range(retries + 1):
            last = attempt == retries
            try:
                return once(body)
            except urllib.error.HTTPError as e:
                transient = e.code == 429 or e.code >= 500
                if not transient or last:
                    cls = VoiceUnavailable if transient or e.code in ACCOUNT_CODES else VoiceError
                    raise cls(f"HeyGen TTS HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
            except (urllib.error.URLError, OSError) as e:  # timeouts and resets are OSErrors too
                if last:
                    raise VoiceUnavailable(f"HeyGen TTS unreachable: {e}") from e
            sleep(retry_wait_s)
        raise AssertionError("unreachable")  # the loop always returns or raises

    return post


def heygen_synth(post: Callable[[dict], tuple[bytes, str | None]], model: str,
                 on_generation: Callable[[str], None]) -> Synth:
    """(text, "<heygen voice>") -> mono float32 at the API's own rate (voice.heygen_sample_rate; never
    resampled here). One request per whole turn. The API returns raw s16le pcm only; every generation id is
    handed on so the billed cost can be read back later."""

    def synth(text: str, voice_name: str) -> np.ndarray:
        pcm, gen_id = post({"model": model, "input": text, "voice": voice_name, "response_format": "pcm"})
        if gen_id:
            on_generation(gen_id)
        return np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0

    return synth


def generation_fetcher(key: str, timeout_s: int) -> Callable[[str], float | None]:
    """GET /generation?id= -> billed total_cost, or None while the stats are not there yet (404, network)."""

    def fetch(gen_id: str) -> float | None:
        req = urllib.request.Request(f"{OPENROUTER_BASE}/generation?id={gen_id}",
                                     headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                cost = json.loads(r.read())["data"]["total_cost"]
        except (OSError, ValueError, KeyError, TypeError):  # HTTPError/URLError are OSErrors
            return None
        return float(cost) if isinstance(cost, int | float) else None

    return fetch


def billed_cost(gen_ids: list[str], fetch: Callable[[str], float | None], attempts: int, wait_s: float,
                sleep: Callable[[float], None] = time.sleep, id_chars: dict[str, int] | None = None,
                price_per_million_chars: float = 0.0) -> tuple[float, int]:
    """Sum the billed cost of every generation -> (usd, ids whose cost could not be read). The stats are
    eventually consistent (a fresh id may 404 for seconds), so ids that miss are retried, `wait_s` apart.

    An id that never reads back WAS billed, so it is counted at the estimated per-character price for its own
    characters (`id_chars`), not as free: a down /generation endpoint must not make the daily cap unreachable.
    The count of such ids is still returned, so the estimate stays visible in voice_costs.missing."""
    found: dict[str, float] = {}
    for attempt in range(attempts):
        for gid in gen_ids:
            if gid not in found and (cost := fetch(gid)) is not None:
                found[gid] = cost
        if len(found) == len(gen_ids):
            break
        if attempt < attempts - 1:
            sleep(wait_s)
    estimated = sum((id_chars or {}).get(g, 0) for g in gen_ids if g not in found) * price_per_million_chars / 1e6
    return sum(found.values()) + estimated, len(gen_ids) - len(found)


SILENT_RMS = 1e-5  # below this a turn is silence (or a decoder artefact): gaining it up would only amplify noise


def match_level(audio: np.ndarray, rms_dbfs: float, peak_dbfs: float) -> np.ndarray:
    """Gain one turn to the target RMS, but never past the peak ceiling, so a loud turn is not clipped (it ends
    up quieter than the target instead; the final loudnorm evens out the whole episode). Silent turns pass."""
    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
    peak = float(np.max(np.abs(audio)))
    if rms < SILENT_RMS or peak == 0.0:
        return audio
    gain = min(10 ** (rms_dbfs / 20) / rms, 10 ** (peak_dbfs / 20) / peak)
    return (audio * gain).astype(np.float32)


def render_turns(turns: list[dict], voices: dict[str, str], out: Path, synth: Synth, sample_rate: int,
                 gap_s: float, speaker_change_gap_s: float, rms_dbfs: float, peak_dbfs: float) -> dict:
    """Voice the turns one request each, in script order. Each turn is level-matched (the two voices sit at
    different levels), then followed by silence: speaker_change_gap_s when the other host speaks next, else
    gap_s (also after the last turn)."""
    for i, t in enumerate(turns):
        if t.get("speaker") not in voices:
            raise VoiceError(f"turn {i}: unknown speaker {t.get('speaker')!r}")
        if not str(t.get("text", "")).strip():
            raise VoiceError(f"turn {i}: empty text")
    parts: list[np.ndarray] = []
    for i, t in enumerate(turns):
        name = voices[t["speaker"]]
        try:
            audio = synth(t["text"].strip(), name)
        # transport boundary: whatever the HTTP layer or a decoder raises must reach the builder as a VoiceError,
        # so it counts against the episode instead of crashing the run
        except VoiceUnavailable:  # the provider's outage keeps its type: it must not be charged to the episode
            raise
        except Exception as e:
            raise VoiceError(f"turn {i}: synthesis with {name} failed: {type(e).__name__}: {e}") from e
        if audio.size == 0:
            raise VoiceError(f"turn {i}: synthesis returned no audio")
        changes = i + 1 < len(turns) and turns[i + 1]["speaker"] != t["speaker"]
        gap = speaker_change_gap_s if changes else gap_s
        parts += [match_level(audio, rms_dbfs, peak_dbfs), np.zeros(int(gap * sample_rate), dtype=np.float32)]
    wav = np.concatenate(parts)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.wav")
    sf.write(tmp, wav, sample_rate)
    tmp.replace(out)
    return {"turns": len(turns), "seconds": round(len(wav) / sample_rate, 2)}
