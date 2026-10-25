"""Housekeeping that outlives any one episode: archive the finished morning, snapshot the state that exists
nowhere else, and prune old local and archive files. Run once per date by the hourly builder (build.main) through run_if_due.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

from daily_brief import db
from daily_brief.config import Config


def archive_show(cfg: Config, date: str) -> dict:
    src = cfg.paths.var_dir / "show" / date
    if not src.is_dir():
        return {"archived": 0}
    dest = cfg.paths.archive_dir / date
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    # READY last: in the archive it doubles as "this copy is complete" (see _archived), so a copy the archive copy was cut
    # short is retried rather than trusted
    for f in sorted((f for f in src.iterdir() if f.is_file()), key=lambda f: f.name == "READY"):
        shutil.copy2(f, dest / f.name)
        n += 1
    return {"archived": n, "to": str(dest)}


def _archived(cfg: Config, date: str) -> bool:
    return (cfg.paths.archive_dir / date / "READY").is_file()


def _local_shows(cfg: Config):
    """(date, dir) of every local assembled morning: a dated folder that holds a READY."""
    base = cfg.paths.var_dir / "show"
    for d in sorted(base.iterdir()) if base.is_dir() else []:
        try:
            dt.date.fromisoformat(d.name)
        except ValueError:
            continue  # not a dated dir (e.g. playlist.m3u)
        if d.is_dir() and (d / "READY").is_file():
            yield d.name, d


def archive_pending(cfg: Config) -> dict:
    """Archive every local show missing from the archive, however old. Yesterday-and-today was not enough: an archive
    outage longer than a day lost the shows in between once the local copies aged out."""
    days = []
    for date, _ in _local_shows(cfg):
        if not _archived(cfg, date):
            archive_show(cfg, date)
            days.append(date)
    return {"archived_days": days}


STATE_TABLES = ("runs", "feedback", "ledger")  # what exists nowhere else: run history, reactions, covered topics


def _state_rows(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return sum(conn.execute(f"select count(*) from {t}").fetchone()[0] for t in STATE_TABLES)
    except sqlite3.DatabaseError:
        return -1  # unreadable or not ours: never trusted over a good copy
    finally:
        conn.close()


def snapshot_state(cfg: Config, date: str, keep_days: int) -> dict:
    """Copy the state that exists nowhere else to the archive: brief.db (feedback, covered-topics ledger, run
    history) and the approved brief + its history.

    - SQLite's locking hangs on the CIFS mount, so every SQLite open happens on local disk; only finished
      files are copied across.
    - Each date writes a dated copy (state/brief-<date>.db, pruned after keep_days).
    - state/brief.db (what the runbook restores from) is replaced only when the new snapshot holds at least
      as many state rows. A host that lost its DB silently gets a fresh empty one from db.connect, and that
      must never overwrite the good backup it is about to need."""
    state = cfg.paths.archive_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    local = cfg.paths.var_dir / "brief.snapshot.db"
    local.unlink(missing_ok=True)
    src = sqlite3.connect(cfg.paths.var_dir / "brief.db")
    try:
        dst = sqlite3.connect(local)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    new_rows = _state_rows(local)
    latest, old_rows = state / "brief.db", -1
    if latest.exists():
        probe = cfg.paths.var_dir / "brief.latest-probe.db"
        shutil.copyfile(latest, probe)
        old_rows = _state_rows(probe)
        probe.unlink()
    shutil.copyfile(local, state / f"brief-{date}.db.tmp")
    (state / f"brief-{date}.db.tmp").replace(state / f"brief-{date}.db")
    if new_rows >= old_rows:
        shutil.copyfile(local, state / "brief.db.tmp")
        (state / "brief.db.tmp").replace(latest)
        verdict = "updated"
    else:
        verdict = f"kept: new snapshot has {new_rows} state rows, the existing copy {old_rows}"
    local.unlink()
    brief_dir = cfg.paths.var_dir / "brief"
    if brief_dir.is_dir():
        shutil.copytree(brief_dir, state / "brief", dirs_exist_ok=True)
    today = dt.date.fromisoformat(date)
    pruned = 0
    for f in state.glob("brief-*.db"):
        try:
            age = (today - dt.date.fromisoformat(f.name.removeprefix("brief-").removesuffix(".db"))).days
        except ValueError:
            continue  # not a dated snapshot: not ours to judge
        if age > keep_days:
            f.unlink()
            pruned += 1
    return {"state_to": str(state), "latest": verdict, "rows": new_rows, "pruned_snapshots": pruned}


def prune(cfg: Config, today: dt.date, keep_work_days: int, keep_show_days: int) -> dict:
    """Delete dated work/ and show/ folders older than their keep window; never touches anything else. A show
    that has not reached the archive yet is kept however old it is: it is the only copy."""
    removed = 0
    for sub, keep in (("work", keep_work_days), ("show", keep_show_days)):
        base = cfg.paths.var_dir / sub
        if not base.is_dir():
            continue
        for d in base.iterdir():
            try:
                age = (today - dt.date.fromisoformat(d.name)).days
            except ValueError:
                continue  # not a dated dir (e.g. playlist.m3u); leave it alone
            if d.is_dir() and age > keep:
                if sub == "show" and (d / "READY").is_file() and not _archived(cfg, d.name):
                    continue
                shutil.rmtree(d)
                removed += 1
    return {"removed_dirs": removed}


def prune_library(cfg: Config, conn: sqlite3.Connection, today: dt.date, keep_days: int) -> dict:
    """Delete var/library/<id>/ for episodes that aired (assigned) or were skipped or found unplayable more than keep_days ago.

    Chosen from the episodes table, never by scanning the folder, so an unassigned ready episode (the queue's
    stock) can't be touched. The rows stay: a skipped row is what keeps the item from being picked again."""
    cutoff = (today - dt.timedelta(days=keep_days)).isoformat()
    ids = [r[0] for r in conn.execute(
        "select item_id from episodes where (assigned_date is not null and assigned_date < ?) "
        "or (status in ('skipped', 'bad') and substr(built_at, 1, 10) < ?)", (cutoff, cutoff))]
    removed = 0
    for item_id in ids:
        d = cfg.paths.var_dir / "library" / str(item_id)
        if d.is_dir():
            shutil.rmtree(d)
            removed += 1
    return {"removed_library_dirs": removed}


def prune_archive(cfg: Config, today: dt.date, keep_days: int) -> dict:
    """archive retention: delete dated show folders older than keep_days; never touches anything else."""
    base = cfg.paths.archive_dir
    removed = 0
    if base.is_dir():
        for d in base.iterdir():
            try:
                age = (today - dt.date.fromisoformat(d.name)).days
            except ValueError:
                continue  # not a dated folder: not ours to judge
            if d.is_dir() and age > keep_days:
                shutil.rmtree(d)
                removed += 1
    return {"removed_archive_days": removed}


def due(conn: sqlite3.Connection, date: str) -> bool:
    """True until an upkeep run has finished ok for this date; a failed one (archive unreachable) is retried by the next hour."""
    return conn.execute("select 1 from runs where date = ? and stage = 'upkeep' and status = 'ok'",
                        (date,)).fetchone() is None


def run_if_due(conn: sqlite3.Connection, cfg: Config, date: str) -> dict | None:
    """Archive, snapshot and prune once per date, recorded as stage 'upkeep'. None when already done today.

    The steps are independent: an archive failure (archive show copy) must not also skip the state snapshot, so
    each runs and its error is collected. The finished morning is assembled after this runs for the day, so the
    previous day's show is archived along with today's (copying is idempotent)."""
    if not due(conn, date):
        return None
    today = dt.date.fromisoformat(date)
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    p = cfg.pipeline
    steps: list[tuple[str, Callable[[], dict]]] = [
        ("archive", lambda: {"yesterday": archive_show(cfg, yesterday), "today": archive_show(cfg, date)}
         | archive_pending(cfg)),
        ("state-snapshot", lambda: snapshot_state(cfg, date, p.archive_keep_days)),
        ("prune", lambda: prune(cfg, today, p.keep_work_days, p.keep_show_days)
         | prune_library(cfg, conn, today, p.keep_library_days)
         | prune_archive(cfg, today, p.archive_keep_days)),
    ]
    run = db.start_run(conn, date, "upkeep")
    meta: dict = {}
    errors: dict[str, str] = {}
    for name, fn in steps:
        try:
            meta[name] = fn()
        # step boundary: one step's failure is recorded and the others still run
        except Exception as e:  # noqa: BLE001
            errors[name] = f"{type(e).__name__}: {e}"
    if errors:
        db.finish_run(conn, run, "failed", error="; ".join(f"{k}: {v}" for k, v in errors.items()), meta=meta)
    else:
        db.finish_run(conn, run, "ok", meta=meta)
    return {"status": "failed" if errors else "ok", "errors": errors or None, **meta}
