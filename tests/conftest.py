"""Suite-wide guard: tests must never write to real runtime or archive locations.

A test once wrote fake shows into the real archive because its fixture kept the config's
archive path. This snapshot-compares those trees around the whole session and fails loudly.
"""

from pathlib import Path

import pytest

PROTECTED = [Path("/archive/daily_brief"), Path.home() / ".local" / "share" / "daily_brief" / "archive",
             Path.home() / ".local" / "share" / "daily_brief" / "var"]


def _snapshot() -> dict[str, float]:
    snap = {}
    for root in PROTECTED:
        if root.is_dir():
            for p in root.rglob("*"):
                try:
                    snap[str(p)] = p.stat().st_mtime
                except OSError:
                    continue
    return snap


@pytest.fixture(scope="session", autouse=True)
def real_locations_untouched():
    before = _snapshot()
    yield
    after = _snapshot()
    # runtime var legitimately changes while the live API/night run beside the tests: only NEW
    # paths under the archive, or new top-level show days anywhere, count as test leakage
    leaked = [p for p in set(after) - set(before)
              if p.startswith(str(PROTECTED[0])) or "/show/" in p]
    assert not leaked, f"tests wrote into real locations: {sorted(leaked)[:5]}"


REPO = Path(__file__).resolve().parent.parent


def write_config(tmp_path: Path, replace: dict[str, str] | None = None) -> Path:
    """The repo config.example.toml with every runtime/archive path redirected into tmp_path, plus optional text replacements
    (each old string must exist, so a renamed key fails the test instead of silently testing nothing)."""
    text = (REPO / "config.example.toml").read_text()
    text = text.replace("/app/var", str(tmp_path / "var"))
    text = text.replace("/archive/daily_brief", str(tmp_path / "archive"))
    text = text.replace("/app/secrets.env", str(tmp_path / "secrets.env"))  # tests write tokens here
    text = text.replace('"/app/brief/brief.md"', f'"{REPO / "brief" / "brief.md"}"')
    for old, new in (replace or {}).items():
        assert old in text, old
        text = text.replace(old, new)
    out = tmp_path / "config.toml"
    out.write_text(text)
    return out


class FakeASR:
    """A stand-in for asr.Transcriber: canned segments, a usage meter, and an optional error raised on call N."""

    def __init__(self, segments=None, cost=0.0003, fail_on_call=None, fail_with=None):
        from daily_brief import asr
        self.segments = segments if segments is not None else [asr.Segment(0, "words " * 400)]
        self.cost, self.fail_on_call, self.fail_with = cost, fail_on_call, fail_with
        self.usage, self.calls = asr.Usage(), []

    def __call__(self, path):
        self.calls.append(path)
        if self.fail_on_call == len(self.calls):
            raise self.fail_with
        self.usage.requests += 1
        self.usage.cost += self.cost
        return self.segments
