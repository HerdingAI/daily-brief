"""Relevance brief: the only thing the editor knows about the listener .

brief.md = YAML front matter (the contract, validated here) + free-text notes (ignored by the
editor). A malformed brief fails loudly, so the builder falls back to the last good release
instead of quietly editing against garbage.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

SOURCES = {"hand", "hand-draft", "profile-agent"}
PRIORITY_ID = re.compile(r"^p\d+$")


class BriefError(ValueError):
    """brief.md is missing a field, has a bad value, or leaks personal terms onto the TV."""


@dataclass(frozen=True)
class Priority:
    id: str
    text: str
    signals: list[str]


@dataclass(frozen=True)
class Brief:
    version: int
    generated: dt.date
    source: str
    tv_safe: bool
    priorities: list[Priority]
    active_projects: list[str]
    already_known: list[str]
    ignore: list[str]
    depth: dict
    tone: str

    def priority_ids(self) -> set[str]:
        return {p.id for p in self.priorities}

    def prompt_text(self) -> str:
        """The brief as the editor sees it: the validated fields only, never the free-text notes."""
        return yaml.safe_dump({
            "priorities": [{"id": p.id, "text": p.text, "signals": p.signals} for p in self.priorities],
            "active_projects": self.active_projects,
            "already_known": self.already_known,
            "ignore": self.ignore,
            "depth": self.depth,
            "tone": self.tone,
        }, sort_keys=False, allow_unicode=True, width=110)


def _str_list(fm: dict, key: str) -> list[str]:
    v = fm.get(key)
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise BriefError(f"{key} must be a list of non-empty strings")
    return v


def _front_matter(text: str) -> dict:
    if not text.startswith("---\n"):
        raise BriefError("brief.md must start with YAML front matter (---)")
    try:
        _, fm, _ = text.split("---\n", 2)
        data = yaml.safe_load(fm)
    except (ValueError, yaml.YAMLError) as e:
        raise BriefError(f"cannot parse front matter: {e}") from e
    if not isinstance(data, dict):
        raise BriefError("front matter must be a YAML mapping")
    return data


def load_brief(path: str | Path, tv_forbidden_terms: list[str]) -> Brief:
    fm = _front_matter(Path(path).read_text())

    version = fm.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise BriefError("version must be a non-negative integer")
    generated = fm.get("generated")
    if not isinstance(generated, dt.date):
        raise BriefError("generated must be a date (YYYY-MM-DD)")
    source = fm.get("source")
    if source not in SOURCES:
        raise BriefError(f"source must be one of {sorted(SOURCES)}, got {source!r}")
    tv_safe = fm.get("tv_safe")
    if not isinstance(tv_safe, bool):
        raise BriefError("tv_safe must be true or false")

    raw_priorities = fm.get("priorities")
    if not isinstance(raw_priorities, list) or not raw_priorities:
        raise BriefError("priorities must be a non-empty list")
    priorities, seen = [], set()
    for i, p in enumerate(raw_priorities):
        if not isinstance(p, dict):
            raise BriefError(f"priorities[{i}] must be a mapping")
        pid, text, signals = p.get("id"), p.get("text"), p.get("signals", [])
        if not isinstance(pid, str) or not PRIORITY_ID.match(pid):
            raise BriefError(f"priorities[{i}].id must look like p<number>, got {pid!r}")
        if pid in seen:
            raise BriefError(f"duplicate priority id {pid}")
        seen.add(pid)
        if not isinstance(text, str) or not text.strip():
            raise BriefError(f"priorities[{i}].text must be non-empty")
        if not isinstance(signals, list) or not all(isinstance(s, str) for s in signals):
            raise BriefError(f"priorities[{i}].signals must be a list of strings")
        priorities.append(Priority(pid, text.strip(), signals))

    depth = fm.get("depth", {})
    if not isinstance(depth, dict):
        raise BriefError("depth must be a mapping")
    tone = fm.get("tone", "")
    if not isinstance(tone, str):
        raise BriefError("tone must be a string")

    b = Brief(version, generated, source, tv_safe, priorities, _str_list(fm, "active_projects"),
              _str_list(fm, "already_known"), _str_list(fm, "ignore"), depth, tone)

    if b.tv_safe:
        blob = b.prompt_text().lower()
        leaked = [t for t in tv_forbidden_terms if t.lower() in blob]
        if leaked:
            raise BriefError(f"tv_safe brief mentions personal terms: {leaked}")
    return b


def runtime_dir(cfg) -> Path:
    return cfg.paths.var_dir / "brief"


def effective_path(cfg) -> Path:
    """The brief the builder uses: the last approved runtime brief, else the git seed (brief.path)."""
    approved = runtime_dir(cfg) / "brief.md"
    return approved if approved.is_file() else cfg.brief.path


def main(argv: list[str]) -> int:
    from daily_brief.config import load_config

    cfg = load_config(argv[2] if len(argv) > 2 else "config.toml")
    path = argv[1] if len(argv) > 1 else cfg.brief.path
    try:
        b = load_brief(path, cfg.brief.tv_forbidden_terms)
    except BriefError as e:
        print(f"brief INVALID: {e}", file=sys.stderr)
        return 1
    print(f"brief OK: v{b.version} ({b.source}, {b.generated}), priorities {sorted(b.priority_ids())}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
