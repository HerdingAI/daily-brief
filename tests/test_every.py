import subprocess
import sys
import threading

import pytest

from daily_brief import every


def run_loop(runner, *, max_runs, clock_values=None, stop=None, interval=60, timeout=90):
    stop = stop or threading.Event()
    waits, lines = [], []
    ticks = iter(clock_values) if clock_values else None
    clock = (lambda: next(ticks)) if ticks else (lambda: 3600 * 10 + 1200.0)
    n = every.loop("/app/config.toml", interval, timeout, runner=runner, clock=clock,
                   wait=lambda s: waits.append(s), stop=stop, max_runs=max_runs, emit=lines.append)
    return n, waits, lines


@pytest.mark.parametrize("now,interval,expected", [
    (3600 * 10 + 1200, 60, 2400),       # 10:20 -> 11:00
    (3600 * 10, 60, 3600),              # exactly on the boundary: a full interval, never a zero-length sleep
    (3600 * 10 + 3599.5, 60, 0.5),
    (1800 * 7 + 100, 30, 1700),
    (900 * 3 + 1, 15, 899),
])
def test_alignment(now, interval, expected):
    assert every.seconds_until_next_boundary(now, interval) == pytest.approx(expected)


def test_runs_the_builder_as_a_child_and_sleeps_to_the_boundary():
    calls = []

    def runner(cmd, timeout_s):
        calls.append((cmd, timeout_s))
        return 0

    n, waits, lines = run_loop(runner, max_runs=2)
    assert n == 2 and len(calls) == 2
    assert calls[0] == ([sys.executable, "-m", "daily_brief.build", "/app/config.toml"], 90 * 60)
    assert waits == [2400.0]                       # one sleep between the two runs, none after the last
    assert [ln for ln in lines if "exit 0" in ln] and "run 1 start" in lines[0]
    assert "next run in 2400s" in lines


def test_a_failing_child_never_stops_the_loop():
    codes = iter([1, 75, 0])
    n, waits, lines = run_loop(lambda cmd, t: next(codes), max_runs=3)
    assert n == 3 and len(waits) == 2
    assert any("run 1 exit 1" in ln for ln in lines) and any("run 2 exit 75" in ln for ln in lines)


def test_a_timeout_is_logged_and_the_loop_goes_on():
    def runner(cmd, t):
        if not runner.seen:
            runner.seen = True
            raise subprocess.TimeoutExpired(cmd, t)
        return 0

    runner.seen = False
    n, _, lines = run_loop(runner, max_runs=2, timeout=5)
    assert n == 2 and any("TIMEOUT after 5 min" in ln for ln in lines) and any("run 2 exit 0" in ln for ln in lines)


def test_a_child_that_cannot_start_is_logged_and_the_loop_goes_on():
    def runner(cmd, t):
        raise FileNotFoundError("python")

    n, waits, lines = run_loop(runner, max_runs=2)
    assert n == 2 and len(waits) == 1 and any("could not start: FileNotFoundError" in ln for ln in lines)


def test_stop_ends_the_loop_after_the_current_run_without_sleeping():
    stop = threading.Event()

    def runner(cmd, t):
        stop.set()                                 # SIGTERM arrives mid-run
        return -15

    n, waits, lines = run_loop(runner, max_runs=None, stop=stop)
    assert n == 1 and waits == [] and "(stopped)" in lines[-1]


def test_stop_set_before_start_runs_nothing():
    stop = threading.Event()
    stop.set()
    n, _, _ = run_loop(lambda c, t: pytest.fail("no run"), max_runs=None, stop=stop)
    assert n == 0


def test_real_runner_returns_the_exit_code_and_kills_on_timeout(monkeypatch):
    run = every.make_runner(threading.Event())
    assert run([sys.executable, "-c", "raise SystemExit(3)"], 30) == 3
    monkeypatch.setattr(every, "STOP_POLL_SECONDS", 0.05)
    monkeypatch.setattr(every, "KILL_GRACE_SECONDS", 2)
    with pytest.raises(subprocess.TimeoutExpired):
        run([sys.executable, "-c", "import time; time.sleep(60)"], 0.3)


def test_real_runner_terminates_the_child_when_stopped(monkeypatch):
    monkeypatch.setattr(every, "STOP_POLL_SECONDS", 0.05)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    code = every.make_runner(stop)([sys.executable, "-c", "import time; time.sleep(60)"], 30)
    assert code != 0                               # killed by SIGTERM, did not wait out the sleep
