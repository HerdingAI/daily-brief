"""Transcriber: episode audio -> timestamped transcript on disk.

Only the item the builder picked is transcribed (never the whole back catalog). Transcripts
are kept permanently so a back-catalog episode is transcribed once; the downloaded audio is
deleted as soon as it has been used.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from types import SimpleNamespace

Download = Callable[[str, Path], None]
Whisper = Callable[[Path], Iterable]  # returns segments with .start (seconds) and .text
MARKER = re.compile(r"\[\d{2,}:\d{2}\]")


def spoken_words(text: str) -> int:
    return len(MARKER.sub(" ", text).split())


class RateLimited(RuntimeError):
    """YouTube answered 429 / blocked us / timed out; stop asking this source for this run."""


class TranscriptDeferred(RuntimeError):
    """No caption source could answer right now (blocked, rate-limited, unreachable). The video may well have a
    transcript, so the item is retried on a later run WITHOUT being charged an attempt."""


def is_youtube(url: str | None) -> bool:
    return bool(url) and ("youtube.com/watch" in url or "youtu.be/" in url)


class CaptionFetcher:
    """One YouTube caption source with a circuit breaker.

    The first RateLimited trips the breaker for the rest of the run (hammering a blocked endpoint only
    extends the block). A tripped source raises TranscriptDeferred instead of answering, so "we couldn't ask"
    is never mistaken for "this video has no transcript". None means the source answered: no transcript.
    """

    def __init__(self, fetch: Callable[[str], str | None]):
        self.fetch = fetch
        self.tripped = False

    def __call__(self, url: str) -> str | None:
        if self.tripped:
            raise TranscriptDeferred("caption source rate-limited earlier in this run")
        try:
            return self.fetch(url)
        except RateLimited as e:
            self.tripped = True
            raise TranscriptDeferred(f"caption source rate-limited: {e}") from e


def first_captions(*fetchers: CaptionFetcher) -> Callable[[str], str | None]:
    """Try each caption source in order; each has its own breaker, so one blocked endpoint never stops the
    others. Returns the first transcript, None when every source ANSWERED without one, and raises
    TranscriptDeferred when none answered and at least one could not be asked (blocked or unreachable)."""
    def fetch(url: str) -> str | None:
        deferred: list[str] = []
        for f in fetchers:
            try:
                text = f(url)
            except TranscriptDeferred as e:
                deferred.append(str(e))
                continue
            # transport boundary: a reset or DNS failure (OSError, incl. every requests exception) is transient,
            # so try the next source. Deliberately NOT Exception: a code bug must fail loudly, not defer forever.
            except OSError as e:
                deferred.append(f"{type(e).__name__}: {e}")
                continue
            if text:
                return text
        if deferred:
            raise TranscriptDeferred("; ".join(deferred)[:300])
        return None
    return fetch


def timeout_session(timeout_s: int):
    """A requests.Session whose every call gets a default timeout. youtube-transcript-api issues its
    requests with none, so a stalled YouTube connection would otherwise hang the run."""
    import requests

    class _TimeoutSession(requests.Session):
        def request(self, method, url, **kwargs):
            kwargs.setdefault("timeout", timeout_s)
            return super().request(method, url, **kwargs)

    return _TimeoutSession()


def video_id(url: str) -> str:
    u = urllib.parse.urlparse(url)
    if u.netloc.endswith("youtu.be"):
        return u.path.strip("/")
    return urllib.parse.parse_qs(u.query).get("v", [""])[0]


def transcript_api_captions(url: str, every_s: int, api=None, timeout_s: int = 30) -> str | None:
    """YouTube's transcript via youtube-transcript-api: a different endpoint from the caption files
    yt-dlp uses, which answered 429 for every auto-captioned video on 2026-09-27 while this worked."""
    import requests
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (
        CouldNotRetrieveTranscript,
        IpBlocked,
        RequestBlocked,
        YouTubeRequestFailed,
    )

    api = api or YouTubeTranscriptApi(http_client=timeout_session(timeout_s))
    try:
        fetched = api.fetch(video_id(url), languages=["en"])
    except (RequestBlocked, IpBlocked, YouTubeRequestFailed) as e:  # blocked or HTTP error (429 ...)
        raise RateLimited(str(e)[:200]) from e
    except requests.exceptions.RequestException as e:  # timeout, reset, DNS: transient, not "no transcript"
        raise RateLimited(f"{type(e).__name__}: {e}"[:200]) from e
    except CouldNotRetrieveTranscript:  # disabled, none in English, unplayable, age-restricted
        return None
    return format_transcript(fetched.snippets, every_s) or None


def vtt_to_transcript(vtt: str, every_s: int) -> str:
    """YouTube auto-captions -> '[mm:ss] text' lines; rolling-window duplicates removed."""
    segments, recent = [], []
    for block in vtt.split("\n\n"):
        lines = block.strip().splitlines()
        ts = next((ln for ln in lines if "-->" in ln), None)
        if ts is None:
            continue
        h, m, sec = ts.split("-->")[0].strip().split(":")
        start = int(h) * 3600 + int(m) * 60 + float(sec)
        for raw in lines[lines.index(ts) + 1:]:
            text = re.sub(r"<[^>]+>", "", raw).strip()
            if not text or text in recent:
                continue
            recent = (recent + [text])[-3:]
            segments.append(SimpleNamespace(start=start, text=text))
    return format_transcript(segments, every_s)


def ytdlp_captions(url: str, workdir: Path, ytdlp: str = "yt-dlp") -> str | None:
    workdir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([ytdlp, "--js-runtimes", "node", "--skip-download", "--write-auto-subs", "--write-subs",
                        "--sub-langs", "en", "--sub-format", "vtt", "-o", str(workdir / "cap.%(ext)s"), url],
                       capture_output=True, text=True, timeout=180, check=False)
    if "429" in r.stderr:
        raise RateLimited(r.stderr.strip()[-200:])
    vtts = list(workdir.glob("cap*.vtt"))
    try:
        return vtts[0].read_text() if vtts else None
    finally:
        for v in vtts:
            v.unlink()


def ytdlp_audio(url: str, dest: Path, ytdlp: str = "yt-dlp") -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([ytdlp, "--js-runtimes", "node", "-f", "bestaudio", "-o", str(dest), "--no-part",
                        "--force-overwrites", url], capture_output=True, text=True, timeout=1800, check=False)
    if r.returncode != 0 or not dest.exists():
        raise OSError(f"yt-dlp audio download failed: {r.stderr.strip()[-300:]}")


def format_transcript(segments: Iterable, every_s: int) -> str:
    lines: list[str] = []
    last_marker = None
    for s in segments:
        text = s.text.strip()
        if not text:
            continue
        if last_marker is None or s.start - last_marker >= every_s:
            m, sec = divmod(int(s.start), 60)
            lines.append(f"[{m:02d}:{sec:02d}] {text}")
            last_marker = s.start
        else:
            lines[-1] += " " + text
    return "\n".join(lines)


def download_any(url: str, dest: Path, timeout_s: int, user_agent: str, ytdlp: str) -> None:
    """Podcast enclosures over HTTP; YouTube videos via yt-dlp (audio only)."""
    if is_youtube(url):
        ytdlp_audio(url, dest, ytdlp)
    else:
        http_download(url, dest, timeout_s, user_agent)


def http_download(url: str, dest: Path, timeout_s: int = 120, user_agent: str = "Mozilla/5.0") -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout_s) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, length=1 << 20)
    tmp.replace(dest)


def make_whisper(model: str, compute_type: str, cpu_threads: int, beam_size: int, vad_filter: bool) -> Whisper:
    from faster_whisper import WhisperModel  # heavy import, only when actually transcribing

    wm = WhisperModel(model, device="cpu", compute_type=compute_type, cpu_threads=cpu_threads)

    def run(path: Path):
        segments, _ = wm.transcribe(str(path), language="en", beam_size=beam_size, vad_filter=vad_filter)
        return segments  # a generator: the decoding happens as it is consumed

    return run


def record_failure(conn: sqlite3.Connection, item_id: int, reason: str, max_attempts: int) -> None:
    conn.execute(
        "update items set attempts = attempts + 1, fail_reason = ?, "
        "status = case when attempts + 1 >= ? then 'failed' else status end where id = ?",
        (reason[:500], max_attempts, item_id))


def transcribe_items(conn: sqlite3.Connection, item_ids: list[int], work_dir: Path, transcripts_dir: Path,
                     download: Download, whisper: Whisper, every_s: int, max_attempts: int,
                     captions: Callable[[str], str | None] | None = None,
                     youtube_audio_fallback: bool = False) -> dict:
    """YouTube items are transcript-only unless youtube_audio_fallback. When YouTube can't be asked right now
    (TranscriptDeferred) the item is deferred with its reason recorded and NO attempt charged; only a
    source that answered "no transcript" counts toward max_attempts. The video is never downloaded."""
    stats = {"ok": 0, "failed": 0, "skipped": 0, "deferred": 0}
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    for item_id in item_ids:
        row = conn.execute("select audio_url, status from items where id = ?", (item_id,)).fetchone()
        done = conn.execute("select 1 from transcripts where item_id = ?", (item_id,)).fetchone()
        if row is None or done or row[1] == "failed":
            stats["skipped"] += 1
            continue
        audio_url = row[0]
        audio = work_dir / "audio" / f"{item_id}.mp3"
        try:
            if not audio_url:
                raise ValueError("item has no audio_url")
            method, text = "whisper", None
            if captions is not None and is_youtube(audio_url):
                try:
                    vtt = captions(audio_url)
                except TranscriptDeferred as e:
                    if not youtube_audio_fallback:
                        conn.execute("update items set fail_reason = ? where id = ?",
                                     (f"deferred (no attempt charged): {e}"[:500], item_id))
                        stats["deferred"] += 1
                        continue
                    vtt = None
                if vtt:
                    text = vtt if vtt.startswith("[") else vtt_to_transcript(vtt, every_s)
                    method = "captions"
            if text is None and is_youtube(audio_url) and not youtube_audio_fallback:
                raise ValueError("YouTube answered: no transcript in English for this video "
                                 "(the video is not downloaded by design)")
            if text is None:
                download(audio_url, audio)
                text = format_transcript(whisper(audio), every_s)
            if not text.strip():
                raise ValueError("transcription produced no text")
            out = transcripts_dir / f"{item_id}.txt"
            tmp = out.with_suffix(".tmp")
            tmp.write_text(text + "\n")
            tmp.replace(out)
            conn.execute("insert into transcripts(item_id, method, path, words) values (?, ?, ?, ?)",
                         (item_id, method, str(out), spoken_words(text)))
            conn.execute("update items set status = 'transcribed', fail_reason = null, attempts = 0 where id = ?", (item_id,))
            stats["ok"] += 1
        # per-item boundary: record why this episode failed and move on to the next one
        except Exception as e:  # noqa: BLE001
            record_failure(conn, item_id, f"{type(e).__name__}: {e}", max_attempts)
            stats["failed"] += 1
        finally:
            audio.unlink(missing_ok=True)
    return stats
