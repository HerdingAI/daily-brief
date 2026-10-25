"""Claude wrapper: prompt + labelled inputs -> validated JSON.

Every LLM output is parsed and validated; invalid output is a failed attempt, never "best
effort". The retry carries the exact validation error so the model can fix its own output.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

Runner = Callable[[str], tuple[int, str, str]]  # prompt -> (returncode, stdout, stderr)
Validator = Callable[[dict], None]              # raises ValueError describing what is wrong

FENCE = re.compile(r"```json\s*(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    """The model did not produce valid output within max_attempts."""


class LLMAuthError(LLMError):
    """Claude is logged out / the token is invalid. Retrying cannot help and no item is at fault."""


def is_auth_error(text: str, patterns: Sequence[str]) -> bool:
    return any(p and p in text for p in patterns)


def claude_runner(command: str, args: list[str], model: str, timeout_s: int,
                  env: dict[str, str] | None = None, auth_patterns: Sequence[str] = ()) -> Runner:
    """env adds to the inherited environment (e.g. CLAUDE_CODE_OAUTH_TOKEN, since a systemd user unit has
    no login session). auth_patterns ride on the runner so call_json can classify a login failure."""
    cmd = [str(Path(command).expanduser()), *args, *(["--model", model] if model else [])]

    def run(prompt: str) -> tuple[int, str, str]:
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout_s, check=False,
                               env=os.environ | env if env else None)
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {timeout_s}s"
        return r.returncode, r.stdout, r.stderr

    run.auth_patterns = list(auth_patterns)  # type: ignore[attr-defined]
    return run


def build_prompt(prompt: str, inputs: dict[str, str]) -> str:
    return prompt.rstrip() + "\n\n" + "\n\n".join(f"=== {k.upper()} ===\n{v}" for k, v in inputs.items())


def parse_json(stdout: str) -> dict:
    m = FENCE.search(stdout)
    text = m.group(1) if m else stdout.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"output was not valid JSON ({e})") from e
    if not isinstance(data, dict):
        # ValueError on purpose: call_json treats ValueError as "bad output, retry with feedback"
        raise ValueError("output JSON must be an object")  # noqa: TRY004
    return data


def call_json(prompt: str, inputs: dict[str, str], validate: Validator, runner: Runner,
              max_attempts: int, auth_patterns: Sequence[str] | None = None) -> tuple[dict, dict]:
    patterns = auth_patterns if auth_patterns is not None else getattr(runner, "auth_patterns", ())
    base = build_prompt(prompt, inputs)
    full, last_error = base, "no attempt made"
    t0 = time.monotonic()
    for attempt in range(1, max_attempts + 1):
        rc, out, err = runner(full)
        if rc != 0:
            if is_auth_error(f"{out}\n{err}", patterns):
                raise LLMAuthError(f"exit {rc}: {(err.strip() or out.strip())[-300:]}")
            # claude -p sometimes reports the reason (e.g. a usage limit) on stdout with an empty stderr
            last_error = f"exit {rc}: {(err.strip() or out.strip())[-300:]}"
            continue
        try:
            data = parse_json(out)
            validate(data)
        except ValueError as e:
            last_error = str(e)
            full = (base + "\n\n=== YOUR PREVIOUS OUTPUT WAS REJECTED ===\n" + last_error +
                    "\nReturn the complete corrected JSON in one ```json fence.")
            continue
        return data, {"attempts": attempt, "seconds": round(time.monotonic() - t0, 1)}
    raise LLMError(f"no valid output after {max_attempts} attempts; last error: {last_error}")
