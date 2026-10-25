"""User-provided credentials from a KEY=value env file (chmod 600, never in git).

Values are returned to the caller and never logged or put in an exception message.
"""

from __future__ import annotations

from pathlib import Path


class SecretMissing(RuntimeError):
    """A required key is absent. Names the key only, never any value."""

    def __init__(self, key: str):
        super().__init__(f"{key} not found in the secrets file")
        self.key = key


def load(path: Path) -> dict[str, str]:
    """Parse KEY=value lines; blanks, `#` comments and malformed lines are skipped; a missing file is {}."""
    try:
        text = Path(path).expanduser().read_text()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip().removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or key.startswith("#"):
            continue
        out[key] = value.strip().strip("\"'")
    return out


def require(path: Path, key: str) -> str:
    value = load(path).get(key)
    if not value:
        raise SecretMissing(key)
    return value
