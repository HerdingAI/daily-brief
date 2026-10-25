"""The builder container's scheduler: `python -m daily_brief.build` as a fresh child process at every
build.interval_minutes boundary of the wall clock.

A child per run returns Whisper's memory each time and means a crash or hang costs one run, never the loop. A child
that fails, cannot start or outlives build.run_timeout_minutes is logged and the loop carries on; SIGTERM (container
stop) ends the child and the loop cleanly.
"""

from __future__ import annotations

import datetime as dt
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable

from daily_brief.config import load_config

KILL_GRACE_SECONDS = 10  # SIGTERM to SIGKILL for a child that will not stop
STOP_POLL_SECONDS = 1.0

Runner = Callable[[list[str], float], int]   # (command, timeout seconds) -> exit code; raises TimeoutExpired


def seconds_until_next_boundary(now: float, interval_minutes: int) -> float:
    """Seconds from `now` (epoch) to the next multiple of the interval, never 0: at an exact boundary the next one
    is a full interval away. Epoch-based, so it is DST-proof and, for 60, lands on the top of the hour."""
    step = interval_minutes * 60
    return (now // step + 1) * step - now


def make_runner(stop: threading.Event) -> Runner:
    def run(cmd: list[str], timeout_s: float) -> int:
        proc = subprocess.Popen(cmd)
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                try:
                    return proc.wait(timeout=STOP_POLL_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
                if stop.is_set():
                    proc.terminate()
                    try:
                        return proc.wait(timeout=KILL_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        return proc.wait()
                if time.monotonic() >= deadline:
                    proc.terminate()
                    try:
                        proc.wait(timeout=KILL_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    raise subprocess.TimeoutExpired(cmd, timeout_s)
        finally:
            if proc.poll() is None:  # an unexpected exception above: never leave the child behind
                proc.kill()
                proc.wait()
    return run


def log(msg: str) -> None:
    print(f"every: {dt.datetime.now().astimezone().isoformat(timespec='seconds')} {msg}", flush=True)


def loop(config_path: str, interval_minutes: int, timeout_minutes: int, *, runner: Runner,
         clock: Callable[[], float] = time.time, wait: Callable[[float], bool | None],
         stop: threading.Event, max_runs: int | None = None, emit: Callable[[str], None] = log) -> int:
    """Run the builder now, then at each boundary, until `stop` is set or `max_runs` runs have happened.
    `wait(seconds)` sleeps and returns early when stop is set (threading.Event.wait). Returns the runs made."""
    cmd = [sys.executable, "-m", "daily_brief.build", config_path]
    runs = 0
    while not stop.is_set() and (max_runs is None or runs < max_runs):
        started = clock()
        emit(f"run {runs + 1} start")
        try:
            code = runner(cmd, timeout_minutes * 60)
            emit(f"run {runs + 1} exit {code} after {clock() - started:.0f}s" + (" (stopped)" if stop.is_set() else ""))
        except subprocess.TimeoutExpired:
            emit(f"run {runs + 1} TIMEOUT after {timeout_minutes} min, killed; the loop continues")
        except OSError as e:  # cannot even start the child: log it, try again at the next boundary
            emit(f"run {runs + 1} could not start: {type(e).__name__}: {e}")
        runs += 1
        if stop.is_set() or (max_runs is not None and runs >= max_runs):
            break
        pause = seconds_until_next_boundary(clock(), interval_minutes)
        emit(f"next run in {pause:.0f}s")
        wait(pause)
    return runs


def main(argv: list[str]) -> int:
    config_path = argv[1] if len(argv) > 1 else "config.toml"
    cfg = load_config(config_path)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log(f"started: a build run every {cfg.build.interval_minutes} min, timeout {cfg.build.run_timeout_minutes} min")
    loop(config_path, cfg.build.interval_minutes, cfg.build.run_timeout_minutes, runner=make_runner(stop),
         wait=stop.wait, stop=stop)
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
