"""Mix: voiced WAV -> loudness-normalised MP3.

An episode is accepted only after the finished file has been probed and is long enough. READY is written by
queue.assemble; the show runner trusts nothing that is not listed there.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

MIX_RATE = 44100


def probe_duration(path: Path) -> float | None:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                        str(path)], capture_output=True, text=True, timeout=60, check=False)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


class MixError(RuntimeError):
    """Mastering failed or produced an unusable file; READY was not touched."""


def master_segment(wav: Path, out: Path, min_s: float, lufs: float, true_peak: float, bitrate: str,
                   intro: Path | None = None, outro: Path | None = None) -> dict:
    parts = [p for p in (intro, wav, outro) if p]
    inputs = [a for p in parts for a in ("-i", str(p))]
    # resample every part to one format first: stings may not match the voice's rate or channel layout
    norm = ";".join(f"[{i}:a:0]aresample={MIX_RATE},aformat=channel_layouts=mono[p{i}]" for i in range(len(parts)))
    joined = "".join(f"[p{i}]" for i in range(len(parts)))
    graph = f"{norm};{joined}concat=n={len(parts)}:v=0:a=1,loudnorm=I={lufs}:TP={true_peak}:LRA=11[out]"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.mp3")
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", graph,
                        "-map", "[out]", "-ar", str(MIX_RATE), "-b:a", bitrate, str(tmp)],
                       capture_output=True, text=True, timeout=1800, check=False)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise MixError(f"ffmpeg failed: {r.stderr[-400:]}")
    seconds = probe_duration(tmp)
    if seconds is None or seconds < min_s:
        tmp.unlink(missing_ok=True)
        raise MixError(f"mastered segment too short ({seconds}s < {min_s}s)")
    tmp.replace(out)
    return {"seconds": round(seconds, 1), "file": out.name}

