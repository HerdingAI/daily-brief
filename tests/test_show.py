import dataclasses
import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from daily_brief import db, queue, show
from daily_brief.config import load_config
from tests.test_queue import add_episode

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.example.toml"
DATE = "2026-09-27"


@pytest.fixture
def env(tmp_path):
    cfg = load_config(REPO_CONFIG)
    var = tmp_path / "var"
    (var / "show" / DATE).mkdir(parents=True)
    durs = {}

    def make(rel, seconds):
        p = var / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"audio")
        durs[p] = seconds
        return p

    return cfg, var, make, (lambda p: durs.get(p))


def at(h, m):
    return dt.datetime(2026, 9, 27, h, m, tzinfo=dt.UTC)


def ready(var, **segments):
    (var / "show" / DATE / "READY").write_text(json.dumps({"segments": segments}))


def test_show_plays_segments_only(env):
    cfg, var, make, probe = env
    a = make(f"show/{DATE}/A.mp3", 600)
    b = make(f"show/{DATE}/B.mp3", 900)
    make("alarm/alarm.mp3", 1800)             # a leftover cache file is never played
    make("backup/backup.mp3", 600)
    ready(var, A="A.mp3", B="B.mp3")
    plan = show.plan_show(cfg, var, DATE, probe)
    assert [(e.kind, e.label, e.path) for e in plan] == [("segment", "A", a), ("segment", "B", b)]


def test_a_missing_segment_is_not_replaced_by_anything(env):
    cfg, var, make, probe = env
    make(f"show/{DATE}/B.mp3", 900)
    make("backup/backup.mp3", 700)
    ready(var, B="B.mp3")
    assert [e.label for e in show.plan_show(cfg, var, DATE, probe)] == ["B"]


def test_short_or_unlisted_segment_is_not_trusted(env):
    cfg, var, make, probe = env
    make(f"show/{DATE}/A.mp3", 5)          # listed but far too short
    make(f"show/{DATE}/B.mp3", 900)        # present but not listed in READY
    ready(var, A="A.mp3")
    with pytest.raises(show.ShowError):
        show.plan_show(cfg, var, DATE, probe)


def test_no_ready_raises(env):
    cfg, var, _make, probe = env
    with pytest.raises(show.ShowError):
        show.plan_show(cfg, var, DATE, probe)


def test_run_show_plays_the_plan_with_a_backstop_timeout_at_any_hour(env):
    cfg, var, make, probe = env
    make(f"show/{DATE}/A.mp3", 600)
    ready(var, A="A.mp3")
    seen = {}

    def player(playlist, timeout_s, volume, ipc):
        seen.update(playlist=Path(playlist).read_text(), timeout=timeout_s, volume=volume)
        return 0

    status = show.run_show(cfg, var, DATE, at(13, 0), probe, player=player)   # no morning window any more
    assert status["played"] is True and seen["timeout"] == 720 and seen["volume"] == cfg.player.volume
    assert "A.mp3" in seen["playlist"]
    np_ = json.loads((var / "now_playing.json").read_text())
    assert np_["date"] == DATE and np_["entries"][0]["kind"] == "segment" and np_["entries"][0]["starts_at_s"] == 0
    # the run row's meta keeps the keys older readers had
    assert {"played", "player_rc", "entries", "fallback", "alarm_source"} <= set(status)


def _main_env(tmp_path, monkeypatch):
    cfg = load_config(REPO_CONFIG)
    cfg = dataclasses.replace(cfg, paths=dataclasses.replace(cfg.paths, var_dir=tmp_path / "var"))
    monkeypatch.setattr(show, "load_config", lambda _p: cfg)
    return cfg, db.connect(tmp_path / "var" / "brief.db")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_main_assembles_the_library_before_playing(tmp_path, monkeypatch):
    cfg, conn = _main_env(tmp_path, monkeypatch)
    add_episode(conn, cfg, 1, "A", seconds=70)
    mp3 = cfg.paths.var_dir / "library" / "1" / "episode.mp3"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=70", str(mp3)], check=True)
    played = []
    monkeypatch.setattr(show, "mpv_player", lambda playlist, *a: played.append(Path(playlist).read_text()) or 0)
    assert show.main(["show", "config.toml", "--alarm"]) == 0
    row = conn.execute("select status, meta from runs where stage = 'show'").fetchone()
    assert row[0] == "ok" and json.loads(row[1])["assembled"] == ["A"]
    assert conn.execute("select assigned_date from episodes").fetchone()[0] is not None
    assert played and "A.mp3" in played[0]


def test_main_with_an_empty_library_fails_the_run_visibly(tmp_path, monkeypatch):
    _main_env(tmp_path, monkeypatch)
    assert show.main(["show", "config.toml", "--alarm"]) == 1
    row = db.connect(tmp_path / "var" / "brief.db").execute("select status, error from runs").fetchone()
    assert row[0] == "failed" and "no playable segment" in row[1]


def test_main_finalizes_the_run_row_when_stopped_by_sigterm(tmp_path, monkeypatch):
    # api.py stops brief-show.service for on-demand play; that SIGTERM used to leave the row 'running' forever
    _main_env(tmp_path, monkeypatch)

    def stopped(*a, **k):
        raise show.Stopped("SIGTERM")

    monkeypatch.setattr(show, "run_show", stopped)
    monkeypatch.setattr(queue, "assemble", lambda *a, **k: {})
    assert show.main(["show", "config.toml"]) == 0
    row = db.connect(tmp_path / "var" / "brief.db").execute(
        "select status, finished, meta from runs where stage = 'show'").fetchone()
    assert row[0] == "ok" and row[1] is not None
    assert json.loads(row[2])["stopped"] == "SIGTERM"
