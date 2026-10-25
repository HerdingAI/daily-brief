import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from daily_brief import alarm_clock as ac
from daily_brief import db
from daily_brief.config import load_config

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.example.toml"
MON = dt.datetime(2026, 10, 5, 6, 0, tzinfo=dt.UTC)


@pytest.fixture
def env(tmp_path):
    cfg = load_config(REPO_CONFIG)
    cfg = replace(cfg, paths=replace(cfg.paths, var_dir=tmp_path / "var"))
    conn = db.connect(tmp_path / "var" / "brief.db")
    return cfg, conn, []


def hooks(calls, podcast_ok=True, podcast_err=None, spotify_err=None):
    def start_podcast():
        calls.append("podcast")
        if podcast_err:
            raise ac.AlarmError(podcast_err)

    def start_spotify(cfg):
        calls.append("spotify")
        if spotify_err:
            raise ac.AlarmError(spotify_err)

    return {"start_podcast": start_podcast, "start_spotify": start_spotify,
            "podcast_ok": lambda cfg, conn, date, now: podcast_ok}


def arm(cfg, **kw):
    base = {"enabled": True, "time": "06:00", "days": [0, 1, 2, 3, 4, 5, 6], "sound": "podcast"}
    ac.save(cfg.paths.var_dir, ac.parse(base | kw))


def last(conn):
    return conn.execute("select stage, status, error, meta from runs order by id desc limit 1").fetchone()


def test_disabled_does_nothing(env):
    cfg, conn, calls = env
    assert ac.tick(cfg, MON, conn, **hooks(calls)) == {"fired": False}
    assert calls == [] and last(conn) is None


def test_fires_chosen_sound_once_and_writes_marker_first(env):
    cfg, conn, calls = env
    arm(cfg, sound="spotify")
    r = ac.tick(cfg, MON, conn, **hooks(calls))
    assert r["fired"] and r["used"] == "spotify" and r["fallback_reason"] is None
    assert calls == ["spotify"]
    assert ac.marker_path(cfg.paths.var_dir, "2026-10-05").exists()
    assert ac.tick(cfg, MON + dt.timedelta(minutes=1), conn, **hooks(calls)) == {"fired": False}
    assert calls == ["spotify"]
    row = last(conn)
    assert row[0] == "alarm" and row[1] == "ok"


def test_marker_is_written_before_the_sound_starts(env):
    cfg, conn, calls = env
    arm(cfg)
    seen = {}

    def boom():
        seen["marker"] = ac.marker_path(cfg.paths.var_dir, "2026-10-05").exists()
        raise KeyboardInterrupt  # a crash mid-start

    with pytest.raises(KeyboardInterrupt):
        ac.tick(cfg, MON, conn, start_podcast=boom, start_spotify=lambda c: None,
                podcast_ok=lambda *a: True)
    assert seen["marker"] is True
    assert ac.tick(cfg, MON, conn, **hooks(calls)) == {"fired": False}    # the crash cannot cause a second firing


def test_podcast_without_audio_falls_back_to_spotify(env):
    cfg, conn, calls = env
    arm(cfg, sound="podcast")
    r = ac.tick(cfg, MON, conn, **hooks(calls, podcast_ok=False))
    assert r["used"] == "spotify" and "no playable" in r["fallback_reason"]
    assert calls == ["spotify"]


def test_spotify_down_falls_back_to_podcast(env):
    cfg, conn, calls = env
    arm(cfg, sound="spotify")
    r = ac.tick(cfg, MON, conn, **hooks(calls, spotify_err="bridge refused"))
    assert r["used"] == "podcast" and "bridge refused" in r["fallback_reason"]
    assert calls == ["spotify", "podcast"]


def test_both_failing_records_failed(env):
    cfg, conn, calls = env
    arm(cfg, sound="spotify")
    r = ac.tick(cfg, MON, conn, **hooks(calls, podcast_ok=False, spotify_err="down"))
    assert r["fired"] and r["used"] is None
    row = last(conn)
    assert row[1] == "failed" and "down" in row[2]


def test_late_boot_inside_window_fires_outside_does_not(env):
    cfg, conn, calls = env
    arm(cfg)
    assert ac.tick(cfg, MON + dt.timedelta(minutes=11), conn, **hooks(calls)) == {"fired": False}
    assert ac.tick(cfg, MON + dt.timedelta(minutes=9), conn, **hooks(calls))["fired"]


# ---- the default podcast gate goes through the queue ----------------------------------------------
def test_podcast_gate_assembles_then_checks_playable(env, monkeypatch):
    cfg, conn, _ = env
    from daily_brief import queue, show
    seen = []
    monkeypatch.setattr(queue, "assemble", lambda c, cf, d: seen.append(("assemble", d)) or {})
    monkeypatch.setattr(show, "plan_show", lambda *a, **k: seen.append("plan") or [object()])
    assert ac.default_podcast_ok(cfg, conn, "2026-10-05", MON) is True
    assert seen == [("assemble", "2026-10-05"), "plan"]    # assemble first, then the playable check


def test_podcast_gate_false_when_nothing_assembles(env):
    cfg, conn, _ = env
    assert ac.default_podcast_ok(cfg, conn, "2026-10-05", MON) is False   # empty library: no READY


def test_assembly_crash_falls_back_to_spotify_instead_of_killing_the_tick(env, monkeypatch):
    cfg, conn, calls = env
    from daily_brief import queue
    arm(cfg, sound="podcast")

    def boom(*a, **k):
        raise queue.QueueError("ffmpeg concat failed")

    monkeypatch.setattr(queue, "assemble", boom)
    r = ac.tick(cfg, MON, conn, start_podcast=lambda: calls.append("podcast"),
                start_spotify=lambda c: calls.append("spotify"))
    assert r["used"] == "spotify" and "could not assemble" in r["fallback_reason"]
    assert calls == ["spotify"]


def test_alarm_podcast_assembles_then_starts(env, monkeypatch):
    from daily_brief import queue
    from tests.test_queue import add_episode
    cfg, conn, calls = env
    add_episode(conn, cfg, 1, "A", seconds=600)
    arm(cfg, sound="podcast")
    real = queue.assemble
    monkeypatch.setattr(queue, "assemble", lambda c, cf, d: real(
        c, cf, d, concat=lambda paths, out: out.write_bytes(b"x"), probe=lambda p: 600.0))
    monkeypatch.setattr(ac, "probe_duration", lambda p: 600.0)
    r = ac.tick(cfg, MON, conn, start_podcast=lambda: calls.append("podcast"),
                start_spotify=lambda c: calls.append("spotify"))
    assert r["used"] == "podcast" and calls == ["podcast"]
    assert (cfg.paths.var_dir / "show" / "2026-10-05" / "READY").exists()
