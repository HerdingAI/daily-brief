import datetime as dt
import shutil
import sqlite3

import pytest

from daily_brief import db, upkeep
from daily_brief.config import load_config
from tests.conftest import write_config

DATE = "2026-09-27"


@pytest.fixture
def env(tmp_path):
    cfg = load_config(write_config(tmp_path))
    return cfg, db.connect(cfg.paths.var_dir / "brief.db")


def test_archive_and_prune(env):
    cfg, _conn = env
    show = cfg.paths.var_dir / "show" / DATE
    show.mkdir(parents=True)
    for name in ("A.mp3", "B.mp3", "READY"):
        (show / name).write_bytes(b"x")
    res = upkeep.archive_show(cfg, DATE)
    assert res["archived"] == 3 and (cfg.paths.archive_dir / DATE / "B.mp3").exists()
    old = cfg.paths.var_dir / "show" / "2026-09-01"
    old.mkdir(parents=True)
    playlist = cfg.paths.var_dir / "show" / "playlist.m3u"
    playlist.write_text("#EXTM3U\n")
    assert upkeep.prune(cfg, dt.date(2026, 9, 27), 7, 7) == {"removed_dirs": 1}
    assert not old.exists() and show.exists()
    assert playlist.exists()  # non-dated files are never pruned


def test_archive_of_a_missing_day_is_a_noop(env):
    cfg, _conn = env
    assert upkeep.archive_show(cfg, DATE) == {"archived": 0}


def test_archive_retention_prunes_only_old_dated_dirs(env):
    cfg, _conn = env
    arch = cfg.paths.archive_dir
    for d in ("2026-05-01", "2026-09-20", "notes"):
        (arch / d).mkdir(parents=True)
        (arch / d / "B.mp3").write_bytes(b"x")
    res = upkeep.prune_archive(cfg, dt.date(2026, 9, 27), keep_days=90)
    assert res == {"removed_archive_days": 1}
    assert not (arch / "2026-05-01").exists() and (arch / "2026-09-20").exists() and (arch / "notes").exists()


def test_archive_retention_tolerates_missing_archive(env):
    cfg, _conn = env
    assert upkeep.prune_archive(cfg, dt.date(2026, 9, 27), keep_days=90) == {"removed_archive_days": 0}


def _feedback(conn, n, note="x"):
    for i in range(n):
        conn.execute("insert into feedback(date, segment, cluster_id, kind, note) values (?, 'B', ?, 'up', ?)",
                     (DATE, f"c{i}", note))


def test_state_snapshot_copies_db_and_brief_to_the_archive(env):
    cfg, conn = env
    _feedback(conn, 1, "eval the trace")
    brief_dir = cfg.paths.var_dir / "brief"
    (brief_dir / "history").mkdir(parents=True)
    (brief_dir / "brief.md").write_text("approved v1")
    (brief_dir / "history" / "v0.md").write_text("old")
    res = upkeep.snapshot_state(cfg, DATE, keep_days=90)
    state = cfg.paths.archive_dir / "state"
    copy = sqlite3.connect(state / "brief.db")
    assert copy.execute("select note from feedback").fetchone()[0] == "eval the trace"  # a consistent, live copy
    assert (state / "brief" / "brief.md").read_text() == "approved v1"
    assert (state / "brief" / "history" / "v0.md").exists()
    assert res["state_to"] == str(state) and not list(state.glob("*.tmp"))
    upkeep.snapshot_state(cfg, DATE, keep_days=90)  # idempotent: re-running overwrites in place


def test_state_snapshot_never_opens_sqlite_on_the_archive_mount(env, monkeypatch):
    # SQLite locking hangs on the CIFS archive mount (2026-09-27: stuck on brief.db.tmp for minutes), so the
    # backup is taken on local disk and only a finished file is copied to the archive
    cfg, _conn = env
    real, opened = sqlite3.connect, []
    monkeypatch.setattr(sqlite3, "connect", lambda p, *a, **k: opened.append(str(p)) or real(p, *a, **k))
    upkeep.snapshot_state(cfg, DATE, keep_days=90)
    assert opened and not [p for p in opened if p.startswith(str(cfg.paths.archive_dir))]
    assert (cfg.paths.archive_dir / "state" / "brief.db").stat().st_size > 0


def test_snapshot_writes_a_dated_copy_and_updates_latest(env):
    cfg, conn = env
    _feedback(conn, 2)
    res = upkeep.snapshot_state(cfg, DATE, keep_days=90)
    state = cfg.paths.archive_dir / "state"
    assert (state / f"brief-{DATE}.db").exists() and (state / "brief.db").exists()
    assert res["latest"] == "updated"


def test_blank_local_db_keeps_the_good_latest_copy(env):
    cfg, conn = env
    _feedback(conn, 3)
    upkeep.snapshot_state(cfg, DATE, keep_days=90)
    conn.close()
    (cfg.paths.var_dir / "brief.db").unlink()          # the host lost its DB (power cut, disk swap)
    fresh = db.connect(cfg.paths.var_dir / "brief.db")  # connect() silently builds an empty schema
    res = upkeep.snapshot_state(cfg, "2026-09-28", keep_days=90)
    fresh.close()
    state = cfg.paths.archive_dir / "state"
    assert res["latest"].startswith("kept")
    assert sqlite3.connect(state / "brief.db").execute("select count(*) from feedback").fetchone()[0] == 3
    assert (state / "brief-2026-09-28.db").exists()  # the blank one is still kept, dated, for forensics


def test_old_dated_snapshots_are_pruned(env):
    cfg, _conn = env
    state = cfg.paths.archive_dir / "state"
    state.mkdir(parents=True)
    (state / "brief-2026-06-01.db").write_bytes(b"old")
    (state / "brief-2026-09-20.db").write_bytes(b"recent")
    (state / "notes.txt").write_text("not ours")
    upkeep.snapshot_state(cfg, DATE, keep_days=30)
    assert not (state / "brief-2026-06-01.db").exists()
    assert (state / "brief-2026-09-20.db").exists() and (state / "notes.txt").exists()


def test_upkeep_runs_once_per_day(env):
    cfg, conn = env
    first = upkeep.run_if_due(conn, cfg, DATE)
    assert first["status"] == "ok" and set(first) >= {"archive", "state-snapshot", "prune"}
    assert (cfg.paths.archive_dir / "state" / f"brief-{DATE}.db").exists()
    assert upkeep.run_if_due(conn, cfg, DATE) is None  # a second hourly run the same day does nothing
    runs = conn.execute("select status from runs where stage = 'upkeep' and date = ?", (DATE,)).fetchall()
    assert runs == [("ok",)]
    assert upkeep.run_if_due(conn, cfg, "2026-09-28")["status"] == "ok"  # the next date is due again


def test_upkeep_archives_yesterday_and_today(env):
    cfg, conn = env
    for day in ("2026-09-26", DATE):
        (cfg.paths.var_dir / "show" / day).mkdir(parents=True)
        (cfg.paths.var_dir / "show" / day / "A.mp3").write_bytes(b"x")
    upkeep.run_if_due(conn, cfg, DATE)
    assert (cfg.paths.archive_dir / "2026-09-26" / "A.mp3").exists() and (cfg.paths.archive_dir / DATE / "A.mp3").exists()


def test_failed_upkeep_step_is_recorded_and_retried_next_hour(env, monkeypatch):
    cfg, conn = env

    def boom(*a, **k):
        raise OSError("archive gone")

    monkeypatch.setattr(upkeep, "archive_show", boom)
    res = upkeep.run_if_due(conn, cfg, DATE)
    assert res["status"] == "failed" and "archive" in res["errors"]
    assert "state-snapshot" in res and "prune" in res  # the other steps still ran
    row = conn.execute("select status, error from runs where stage = 'upkeep'").fetchone()
    assert row[0] == "failed" and "archive gone" in row[1]
    monkeypatch.undo()
    assert upkeep.run_if_due(conn, cfg, DATE)["status"] == "ok"  # not marked done: the next hour retries


# ---- library retention ------------------------------------------------------------------------
def _episode(conn, cfg, item_id, status="ready", assigned=None, built="2026-09-27T05:00:00-05:00"):
    conn.execute("insert or ignore into sources(id, segment, kind, url, name) values (1, 'A', 'rss', 'https://x.test', 'S')")
    conn.execute("insert into items(id, source_id, guid, title) values (?, 1, ?, 't')", (item_id, f"g{item_id}"))
    conn.execute("insert into episodes(item_id, segment, status, built_at, assigned_date) values (?, 'A', ?, ?, ?)",
                 (item_id, status, built, assigned))
    d = cfg.paths.var_dir / "library" / str(item_id)
    d.mkdir(parents=True)
    (d / "episode.mp3").write_bytes(b"x")
    return d


def test_library_prune_deletes_old_aired_and_old_skipped_but_never_a_waiting_episode(env):
    cfg, conn = env
    today = dt.date(2026, 10, 27)                                    # 30-day window: cutoff 2026-09-27
    aired_old = _episode(conn, cfg, 1, assigned="2026-09-20")
    aired_recent = _episode(conn, cfg, 2, assigned="2026-10-20")
    on_cutoff = _episode(conn, cfg, 3, assigned="2026-09-27")       # exactly N days ago: kept
    waiting_old = _episode(conn, cfg, 4, built="2026-01-01T05:00:00-05:00")   # unassigned, however old
    skipped_old = _episode(conn, cfg, 5, status="skipped", built="2026-08-01T05:00:00-05:00")
    skipped_new = _episode(conn, cfg, 6, status="skipped", built="2026-10-26T05:00:00-05:00")
    assert upkeep.prune_library(cfg, conn, today, 30) == {"removed_library_dirs": 2}
    assert not aired_old.exists() and not skipped_old.exists()
    assert all(d.exists() for d in (aired_recent, on_cutoff, waiting_old, skipped_new))
    assert conn.execute("select count(*) from episodes").fetchone()[0] == 6    # rows stay: a skip must not be re-picked
    assert upkeep.prune_library(cfg, conn, today, 30) == {"removed_library_dirs": 0}   # idempotent


def test_library_prune_ignores_unknown_folders_and_missing_dirs(env):
    cfg, conn = env
    (cfg.paths.var_dir / "library" / "scratch").mkdir(parents=True)
    d = _episode(conn, cfg, 1, assigned="2026-01-01")
    shutil.rmtree(d)   # the row outlived its folder (already pruned, or never written)
    assert upkeep.prune_library(cfg, conn, dt.date(2026, 10, 27), 30) == {"removed_library_dirs": 0}
    assert (cfg.paths.var_dir / "library" / "scratch").exists()


def test_upkeep_run_prunes_the_library_with_the_configured_window(env):
    cfg, conn = env
    old = _episode(conn, cfg, 1, assigned="2026-01-01")
    res = upkeep.run_if_due(conn, cfg, "2026-10-27")
    assert res["status"] == "ok" and res["prune"]["removed_library_dirs"] == 1 and not old.exists()


def _local_show(cfg, day, ready=True):
    d = cfg.paths.var_dir / "show" / day
    d.mkdir(parents=True)
    for name in ("A.mp3", "B.mp3") + (("READY",) if ready else ()):
        (d / name).write_bytes(b"x")
    return d


def test_upkeep_archives_every_show_missing_from_the_archive_after_a_long_outage(env):
    cfg, conn = env
    for day in ("2026-09-18", "2026-09-22", "2026-09-26", DATE):
        _local_show(cfg, day)
    (cfg.paths.archive_dir / "2026-09-22").mkdir(parents=True)          # already archived: has its READY
    (cfg.paths.archive_dir / "2026-09-22" / "READY").write_bytes(b"x")
    res = upkeep.run_if_due(conn, cfg, DATE)
    assert res["status"] == "ok"
    for day in ("2026-09-18", "2026-09-26", DATE):
        assert (cfg.paths.archive_dir / day / "A.mp3").exists() and (cfg.paths.archive_dir / day / "READY").exists()
    assert not (cfg.paths.archive_dir / "2026-09-22" / "A.mp3").exists()   # not copied again


def test_a_show_whose_archive_copy_was_cut_short_is_archived_again(env):
    cfg, _conn = env
    _local_show(cfg, "2026-09-18")
    (cfg.paths.archive_dir / "2026-09-18").mkdir(parents=True)
    (cfg.paths.archive_dir / "2026-09-18" / "A.mp3").write_bytes(b"x")    # the archive dropped before READY was copied
    assert upkeep.archive_pending(cfg)["archived_days"] == ["2026-09-18"]
    assert (cfg.paths.archive_dir / "2026-09-18" / "READY").exists()
    assert upkeep.archive_pending(cfg)["archived_days"] == []


def test_prune_never_removes_a_local_show_that_is_not_archived_yet(env):
    cfg, _conn = env
    unarchived = _local_show(cfg, "2026-09-10")
    archived = _local_show(cfg, "2026-09-11")
    (cfg.paths.archive_dir / "2026-09-11").mkdir(parents=True)
    (cfg.paths.archive_dir / "2026-09-11" / "READY").write_bytes(b"x")
    unfinished = _local_show(cfg, "2026-09-12", ready=False)               # never assembled: nothing to lose
    assert upkeep.prune(cfg, dt.date(2026, 9, 27), 7, 7) == {"removed_dirs": 2}
    assert unarchived.exists() and not archived.exists() and not unfinished.exists()


def test_a_down_nas_keeps_old_shows_through_the_whole_upkeep_run(env, monkeypatch):
    cfg, conn = env
    old = _local_show(cfg, "2026-09-10")

    def nas_down(cfg_, date):
        raise OSError("archive gone")

    monkeypatch.setattr(upkeep, "archive_show", nas_down)
    res = upkeep.run_if_due(conn, cfg, DATE)
    assert res["status"] == "failed" and old.exists()
