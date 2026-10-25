"""Speech to text via OpenRouter (qwen/qwen3-asr-0.6b): episode audio -> timestamped segments, plus the billed cost.

The audio is always transcoded to small mono mp3 chunks first (a podcast is tens of MB, the API takes a bounded
upload), each chunk is posted on its own, and the segment times of chunk N are shifted by where that chunk starts
in the episode. Spend is accumulated in `Transcriber.usage` as each chunk answers, so a failure on chunk 5 still
leaves the bill of chunks 1-4 for the caller to book.
"""

from __future__ import annotations

import csv
import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from daily_brief import voice


class TranscriptionError(RuntimeError):
    """The audio could not be transcribed (bad request, malformed answer, no audio): the item's own failure."""


class TranscriptionUnavailable(TranscriptionError):
    """The provider, not the item, is at fault: still failing after the retries (429/5xx/timeout/unreachable) or
    refusing the account (401/402/403). Nothing is charged to the item and the run stops trying further ones."""


@dataclass
class Segment:
    start: float  # seconds from the start of the whole episode
    text: str


@dataclass
class Usage:
    cost: float = 0.0   # billed usd, summed over the chunks that answered
    missing: int = 0    # answered chunks whose usage.cost was absent (billed, amount unknown)
    requests: int = 0   # chunks that answered

    def take(self) -> Usage:
        """Hand over what has accumulated and start again from zero."""
        taken = Usage(self.cost, self.missing, self.requests)
        self.cost, self.missing, self.requests = 0.0, 0, 0
        return taken


Post = Callable[[bytes, str], dict]  # (audio bytes, file name) -> the parsed JSON answer


def multipart(fields: dict[str, str], file_field: str, filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f"Content-Type: audio/mpeg\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def asr_poster(model: str, key: str, timeout_s: int, retries: int, retry_wait_s: float,
               sleep: Callable[[float], None] = time.sleep) -> Post:
    """POST one chunk -> parsed verbose_json answer. Transient trouble (URLError, timeout, 429, 5xx) is retried up to
    `retries` times, then raises TranscriptionUnavailable. 401/402/403 (bad key, out of credit) are
    TranscriptionUnavailable at once; any other 4xx or an unreadable answer is the item's own TranscriptionError."""

    def once(data: bytes, filename: str) -> bytes:
        body, ctype = multipart({"model": model, "response_format": "verbose_json"}, "file", filename, data)
        req = urllib.request.Request(f"{voice.OPENROUTER_BASE}/audio/transcriptions", data=body,
                                     headers={"Content-Type": ctype, "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return r.read()

    def post(data: bytes, filename: str) -> dict:
        for attempt in range(retries + 1):
            last = attempt == retries
            try:
                raw = once(data, filename)
            except urllib.error.HTTPError as e:
                transient = e.code == 429 or e.code >= 500
                if not transient or last:
                    cls = TranscriptionUnavailable if transient or e.code in voice.ACCOUNT_CODES else TranscriptionError
                    raise cls(f"ASR HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
            except (urllib.error.URLError, OSError) as e:  # timeouts and resets are OSErrors too
                if last:
                    raise TranscriptionUnavailable(f"ASR unreachable: {e}") from e
            else:
                try:
                    out = json.loads(raw)
                except ValueError as e:
                    raise TranscriptionError(f"ASR answer is not JSON: {raw[:200]!r}") from e
                if not isinstance(out, dict):
                    raise TranscriptionError(f"ASR answer is not an object: {raw[:200]!r}")
                return out
            sleep(retry_wait_s)
        raise AssertionError("unreachable")  # the loop always returns or raises

    return post


class Transcriber:
    """Path -> [Segment]: chunk with ffmpeg, post each chunk, shift segment starts by the chunk's start."""

    def __init__(self, post: Post, chunk_seconds: int, ffmpeg: str = "ffmpeg"):
        self.post, self.chunk_seconds, self.ffmpeg = post, chunk_seconds, ffmpeg
        self.usage = Usage()

    def chunks(self, path: Path, out_dir: Path) -> list[tuple[Path, float]]:
        """Transcode to mono 16 kHz ~32 kbps mp3 chunks -> [(chunk file, its start in the episode)]."""
        listing = out_dir / "chunks.csv"
        r = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-vn",
                            "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k", "-f", "segment",
                            "-segment_time", str(self.chunk_seconds), "-reset_timestamps", "1",
                            "-segment_list", str(listing), "-segment_list_type", "csv",
                            str(out_dir / "chunk%05d.mp3")], capture_output=True, text=True, check=False)
        if r.returncode != 0 or not listing.exists():
            raise TranscriptionError(f"ffmpeg chunking failed: {r.stderr.strip()[-300:]}")
        with open(listing, newline="") as f:
            found = [(out_dir / row[0], float(row[1])) for row in csv.reader(f) if len(row) >= 3]
        if not found:
            raise TranscriptionError("ffmpeg produced no audio chunks")
        return found

    def __call__(self, path: Path) -> list[Segment]:
        segments: list[Segment] = []
        with tempfile.TemporaryDirectory(dir=path.parent, prefix="asr-") as tmp:  # the work dir is the writable one
            for chunk, offset in self.chunks(path, Path(tmp)):
                answer = self.post(chunk.read_bytes(), chunk.name)
                self.usage.requests += 1
                cost = (answer.get("usage") or {}).get("cost") if isinstance(answer.get("usage"), dict) else None
                if isinstance(cost, int | float) and not isinstance(cost, bool):
                    self.usage.cost += float(cost)
                else:
                    self.usage.missing += 1
                segments += self._segments(answer, offset)
        return segments

    @staticmethod
    def _segments(answer: dict, offset: float) -> list[Segment]:
        text = answer.get("text")
        if not isinstance(text, str):
            raise TranscriptionError(f"ASR answer has no text: {str(answer)[:200]}")
        raw = answer.get("segments")
        if isinstance(raw, list) and raw:
            try:
                return [Segment(offset + float(s["start"]), str(s["text"])) for s in raw]
            except (KeyError, TypeError, ValueError) as e:
                raise TranscriptionError(f"ASR segments are malformed: {str(raw)[:200]}") from e
        return [Segment(offset, text)] if text.strip() else []  # no segment detail: the whole chunk at its start


def make_transcriber(model: str, key: str, timeout_s: int, retries: int, retry_wait_s: float, chunk_seconds: int,
                     ffmpeg: str = "ffmpeg", sleep: Callable[[float], None] = time.sleep) -> Transcriber:
    return Transcriber(asr_poster(model, key, timeout_s, retries, retry_wait_s, sleep), chunk_seconds, ffmpeg)
