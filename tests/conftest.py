"""Suite-wide guard: tests must never write to real runtime or archive locations.

A test once wrote fake shows into the real archive because its fixture kept the config's
archive path. This snapshot-compares those trees around the whole session and fails loudly.
"""

from pathlib import Path

import pytest

PROTECTED = [Path.home() / ".local" / "share" / "daily_brief" / "archive", Path.home() / ".local" / "share" / "daily_brief" / "var"]


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
    text = text.replace("~/.local/share/daily_brief/var", str(tmp_path / "var"))
    text = text.replace("~/.local/share/daily_brief/archive", str(tmp_path / "archive"))
    text = text.replace("~/.config/daily_brief/secrets.env", str(tmp_path / "secrets.env"))  # tests write tokens here
    text = text.replace('path = "brief/brief.md"', f'path = "{REPO / "brief" / "brief.md"}"')
    for old, new in (replace or {}).items():
        assert old in text, old
        text = text.replace(old, new)
    out = tmp_path / "config.toml"
    out.write_text(text)
    return out
