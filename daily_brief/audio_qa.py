"""On-demand audio check: transcribe a built episode.mp3 and measure the word error rate against its script.

    python -m daily_brief.audio_qa config.toml var/library/<item_id>

Prints {"wer": x, "words": n} (n = words in the script) and exits 1 when wer exceeds [qa] max_wer. It is a
check for a person to run after a voice or mix change, not a step of the build: it loads the whisper model.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

from daily_brief import transcribe
from daily_brief.config import load_config

EXIT_OVER = 1


def normalise(text: str) -> list[str]:
    """Words only: lowercase, punctuation gone. Hyphens split ("game-changer" and "game changer" are the same
    words to a listener) and apostrophes vanish. Numbers are left as written: a spoken "twenty" against a
    scripted "20" counts as the error it is."""
    text = re.sub(r"[-–—]", " ", text.lower()).replace("'", "").replace("’", "")
    return re.sub(r"[^\w\s]", " ", text).split()


def edit_distance(ref: list[str], hyp: list[str]) -> int:
    """Word-level Levenshtein (substitution, insertion, deletion each cost 1), two rows of memory."""
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1]


def wer(reference: str, hypothesis: str) -> tuple[float, int]:
    """(word error rate, reference word count). An empty script has no meaningful rate."""
    ref, hyp = normalise(reference), normalise(hypothesis)
    if not ref:
        raise ValueError("the script has no words to compare against")
    return edit_distance(ref, hyp) / len(ref), len(ref)


def check_episode(item_dir: Path, whisper: transcribe.Whisper) -> dict:
    meta = json.loads((item_dir / "episode.json").read_text())
    script_text = " ".join(t["text"] for t in meta["turns"])
    spoken = " ".join(seg.text for seg in whisper(item_dir / "episode.mp3"))
    rate, words = wer(script_text, spoken)
    return {"wer": round(rate, 4), "words": words}


def main(argv: list[str], make_whisper: Callable[[], transcribe.Whisper] | None = None) -> int:
    if len(argv) != 3:
        print("usage: python -m daily_brief.audio_qa <config.toml> <library item dir>", file=sys.stderr)
        return 2
    cfg = load_config(argv[1])
    t = cfg.transcribe
    make = make_whisper or (lambda: transcribe.make_whisper(t.model, t.compute_type, t.cpu_threads, t.beam_size,
                                                            t.vad_filter))
    result = check_episode(Path(argv[2]), make())
    print(json.dumps(result))
    return EXIT_OVER if result["wer"] > cfg.qa.max_wer else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
