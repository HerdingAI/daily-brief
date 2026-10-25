"""Is Claude logged in? check_llm makes one tiny model call; auth_status asks `claude auth status` (no call)."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from daily_brief import llm


class PreflightError(RuntimeError):
    """Claude could not be checked (a failing LLM call, or `claude auth status` unreadable)."""


def check_llm(runner: llm.Runner, patterns: Sequence[str]) -> None:
    """One tiny prompt. A login failure is LLMAuthError (stops the run); anything else non-zero is PreflightError."""
    rc, out, err = runner("Reply with OK.")
    if rc == 0:
        return
    text = f"{out}\n{err}"
    if llm.is_auth_error(text, patterns):
        raise llm.LLMAuthError(text.strip()[-300:])
    raise PreflightError(f"claude preflight failed: exit {rc}: {(err.strip() or out.strip())[-300:]}")


def auth_status(command: str, env: dict[str, str] | None, run=subprocess.run, timeout_s: int = 30) -> dict:
    """`claude auth status` -> {"loggedIn", "authMethod", ...}. Makes no model call."""
    cmd = [str(Path(command).expanduser()), "auth", "status"]
    try:
        r = run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False, env=os.environ | (env or {}))
        return json.loads(r.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        raise PreflightError(f"claude auth status unreadable: {type(e).__name__}") from e
