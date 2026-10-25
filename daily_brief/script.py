"""Validation of the one-call episode output (prompts/episode.md).

validate() raises ValueError, so llm.call_json retries once with the exact problem fed back. Every rule is
a hard one: an episode is built once and aired as-is, so a script that breaks a style rule is re-written
now rather than tolerated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from daily_brief.config import Build

MAX_CHAPTERS = 8
MAX_TOPIC_CHARS = 60

# what a text-to-speech voice would read aloud as noise: markdown marks, links, list bullets, emoji
MARKDOWN = re.compile(r"[*#`_~]|\]\(|^\s*(?:[-•>]|\d+[.)])\s", re.MULTILINE)
URL = re.compile(r"https?://|www\.", re.IGNORECASE)
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


@dataclass(frozen=True)
class Rules:
    words_per_minute: int
    min_minutes: float
    max_minutes: float
    max_over: float
    max_turn_words: int
    max_same_speaker_run: int
    hype_words: list[str]
    priority_ids: frozenset[str]

    @classmethod
    def from_config(cls, build: Build, priority_ids: set[str] | frozenset[str]) -> Rules:
        return cls(build.words_per_minute, build.min_minutes, build.max_minutes, build.max_over,
                   build.max_turn_words, build.max_same_speaker_run, list(build.hype_words), frozenset(priority_ids))


    def with_max(self, max_minutes: float) -> Rules:
        """The same rules with a per-episode maximum (build.episode_max_minutes), so the validator and the
        prompt's budget text agree on the length."""
        return replace(self, max_minutes=max_minutes)


def _nonempty_str(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _check_spoken(text: str, where: str) -> None:
    if MARKDOWN.search(text):
        raise ValueError(f"{where} contains markdown or a list marker; write plain spoken sentences")
    if URL.search(text):
        raise ValueError(f"{where} contains a URL; say the name instead")
    if EMOJI.search(text):
        raise ValueError(f"{where} contains an emoji")


def validate_skip(d: dict) -> None:
    if not _nonempty_str(d.get("reason")):
        raise ValueError('a skip needs a "reason": one sentence on why nothing clears the listener\'s brief')


def _validate_chapters(chapters, priority_ids: frozenset[str]) -> None:
    if not isinstance(chapters, list) or not 1 <= len(chapters) <= MAX_CHAPTERS:
        raise ValueError(f"chapters must be a list of 1 to {MAX_CHAPTERS} items")
    for n, c in enumerate(chapters):
        if not isinstance(c, dict):
            # ValueError on purpose: call_json treats ValueError as "bad output, retry with feedback"
            raise ValueError(f"chapters[{n}] must be an object")  # noqa: TRY004
        if not _nonempty_str(c.get("topic")) or len(c["topic"]) > MAX_TOPIC_CHARS:
            raise ValueError(f"chapters[{n}].topic must be a short label (<= {MAX_TOPIC_CHARS} chars)")
        if not _nonempty_str(c.get("why")):
            raise ValueError(f"chapters[{n}].why must be non-empty")
        if c.get("priority") not in priority_ids:
            raise ValueError(f"chapters[{n}].priority must be one of the brief's {sorted(priority_ids)}")


def _validate_turns(turns, rules: Rules) -> None:
    if not isinstance(turns, list) or not turns:
        raise ValueError("turns must be a non-empty list")
    words_total, run = 0, 0
    for n, t in enumerate(turns):
        if not isinstance(t, dict) or t.get("speaker") not in ("A", "B"):
            raise ValueError(f"turns[{n}].speaker must be A or B")
        if not _nonempty_str(t.get("text")):
            raise ValueError(f"turns[{n}].text must be non-empty")
        words = len(t["text"].split())
        if words > rules.max_turn_words:
            raise ValueError(f"turns[{n}] is {words} words (max {rules.max_turn_words}); split it between the hosts")
        _check_spoken(t["text"], f"turns[{n}]")
        words_total += words
        run = run + 1 if n and t["speaker"] == turns[n - 1]["speaker"] else 1
        if run > rules.max_same_speaker_run:
            raise ValueError(f"turns[{n}]: the same speaker for more than {rules.max_same_speaker_run} turns in a row")
    spoken = " ".join(t["text"].lower() for t in turns)
    for w in rules.hype_words:
        if re.search(rf"\b{re.escape(w.lower())}\b", spoken):
            raise ValueError(f"hype word used: {w!r}")
    low = rules.min_minutes * rules.words_per_minute
    high = rules.max_minutes * (1 + rules.max_over) * rules.words_per_minute
    if words_total > high:
        raise ValueError(f"the script is {words_total} words, over the {high:.0f}-word limit "
                         f"({rules.max_minutes:g} minutes plus {rules.max_over:.0%}); cut the weakest material")
    if words_total < low:
        raise ValueError(f"the script is {words_total} words, under the {low:.0f}-word minimum "
                         f"({rules.min_minutes:g} minutes); if the episode cannot fill that with material that "
                         'clears the brief, return {"skip": true, "reason": ...} instead of padding')


def validate(d: dict, rules: Rules) -> None:
    """Accept {"skip": true, "reason"} or a full episode; anything else is rejected with the reason."""
    if d.get("skip") is True:
        validate_skip(d)
        return
    for key in ("title", "why", "so_what"):
        if not _nonempty_str(d.get(key)):
            raise ValueError(f"{key} must be a non-empty string")
    _check_spoken(d["why"], "why")
    _validate_chapters(d.get("chapters"), rules.priority_ids)
    for n, c in enumerate(d["chapters"]):
        _check_spoken(c["why"], f"chapters[{n}].why")
    _validate_turns(d.get("turns"), rules)
