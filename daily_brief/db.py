"""SQLite state store.

One file, WAL mode, foreign keys on. Schema version lives in PRAGMA user_version; a DB written by
a newer release is refused rather than silently misread.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 6
RUN_STATUSES = {"running", "ok", "failed", "skipped"}

SCHEMA_V1 = """
create table sources (
  id integer primary key,
  segment text not null check (segment in ('A','B')),
  kind text not null check (kind in ('rss','youtube')),
  url text not null unique,
  name text not null,
  enabled integer not null default 1,
  backfill integer not null default 0
);
create table items (
  id integer primary key,
  source_id integer not null references sources(id),
  guid text not null,
  title text not null,
  url text,
  audio_url text,
  published_at text,
  duration_s integer,
  status text not null default 'discovered'
    check (status in ('discovered','transcribed','extracted','failed')),
  fail_reason text,
  attempts integer not null default 0,
  is_backcatalog integer not null default 0,
  discovered_at text not null default (datetime('now')),
  unique (source_id, guid)
);
create table transcripts (
  item_id integer primary key references items(id),
  method text not null check (method in ('rss','captions','whisper')),
  path text not null,
  words integer not null,
  created_at text not null default (datetime('now'))
);
create table ideas (
  id integer primary key,
  item_id integer not null references items(id),
  local_id text not null,
  claim text not null,
  evidence_quote text,
  ts text,
  speaker text,
  kind text,
  specificity integer,
  example_in_source text,
  created_at text not null default (datetime('now')),
  unique (item_id, local_id)
);
create table shows (
  date text primary key,
  status text not null default 'planned' check (status in ('planned','ready','partial','failed','played')),
  ready_at text,
  fallback_used text,
  notes text
);
create table rundowns (
  date text not null,
  segment text not null check (segment in ('A','B')),
  json text not null,
  brief_version integer,
  primary key (date, segment)
);
create table rundown_items (
  date text not null,
  segment text not null check (segment in ('A','B')),
  cluster_id text not null,
  idea_ids text not null,
  kept integer not null,
  scores text,
  reason text,
  position integer,
  primary key (date, segment, cluster_id)
);
create table ledger (
  topic_key text primary key,
  first_covered text not null,
  last_covered text not null,
  times integer not null default 1,
  last_delta_note text
);
create table feedback (
  id integer primary key,
  date text not null,
  segment text not null check (segment in ('A','B')),
  cluster_id text not null,
  kind text not null check (kind in ('up','skip_topic','deeper')),
  note text,
  at text not null default (datetime('now'))
);
create table runs (
  id integer primary key,
  date text not null,
  stage text not null,
  status text not null check (status in ('running','ok','failed','skipped')),
  started text not null,
  finished text,
  error text,
  meta text
);
create index runs_date_stage on runs(date, stage);
"""

SCHEMA_V2 = """
alter table rundown_items add column topic text;
"""

SCHEMA_V3 = """
alter table ideas add column parts text;  -- JSON [{name, definition, example}] for named frameworks, else null
"""

SCHEMA_V4 = """
create table voice_costs (  -- billed cloud-voice spend per segment attempt, kept even when the segment fell back
  id integer primary key,
  date text not null,
  segment text not null,
  engine text not null,
  chars integer not null,
  requests integer not null,
  billed_usd real not null,
  missing integer not null,  -- generations whose billed cost could not be read back
  at text not null
);
create index voice_costs_date on voice_costs(date);
"""

SCHEMA_V5 = """
create table episodes (  -- one row per built (or set-aside) episode; ready ones are the library the morning queue draws on
  item_id integer primary key references items(id),
  segment text not null,
  status text not null check (status in ('ready','skipped','bad')),  -- skipped = the model judged it off-brief; bad = the file is unplayable (why holds the reason)
  title text,
  why text,                  -- written TO the listener; shown on the phone
  chapters text,             -- JSON [{topic, why, priority}]
  so_what text,
  path text,                 -- var/library/<item_id>/episode.mp3
  seconds real,
  chars integer,
  billed_usd real,
  built_at text not null,
  assigned_date text         -- the morning this episode was queued for; null = still waiting
);
create index episodes_queue on episodes(segment, status, assigned_date);

-- Items the v1 night already aired (they have ideas, or sit in a rundown as idea ids "e<item>.<n>") must never be
-- rebuilt as new episodes. built_at is the item's own date, not now: a row dated today would use up today's
-- build.max_new_per_source_per_day slot of every creator that aired.
insert into episodes(item_id, segment, status, why, built_at)
  select i.id, s.segment, 'skipped', 'aired by v1', coalesce(i.published_at, i.discovered_at)
  from items i join sources s on s.id = i.source_id
  where exists (select 1 from ideas d where d.item_id = i.id)
     or exists (select 1 from rundown_items r where r.idea_ids like '%"e' || i.id || '.%');
"""

SCHEMA_V6 = """
-- transcripts.method gains 'asr' (OpenRouter speech-to-text replaced local Whisper). SQLite cannot alter a CHECK,
-- so the table is rebuilt; old 'whisper' rows stay valid.
create table transcripts_new (
  item_id integer primary key references items(id),
  method text not null check (method in ('rss','captions','whisper','asr')),
  path text not null,
  words integer not null,
  created_at text not null default (datetime('now'))
);
insert into transcripts_new(item_id, method, path, words, created_at)
  select item_id, method, path, words, created_at from transcripts;
drop table transcripts;
alter table transcripts_new rename to transcripts;
"""

MIGRATIONS = {1: SCHEMA_V1, 2: SCHEMA_V2, 3: SCHEMA_V3, 4: SCHEMA_V4, 5: SCHEMA_V5, 6: SCHEMA_V6}  # version -> DDL that upgrades from version-1


class SchemaError(RuntimeError):
    """The database schema is newer than this code understands."""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit; explicit transactions below
    conn.execute("pragma busy_timeout=5000")  # first: switching to WAL takes a lock too
    _enable_wal(conn)
    conn.execute("pragma foreign_keys=on")
    current = _user_version(conn)
    if current > SCHEMA_VERSION:
        conn.close()
        raise SchemaError(f"{path} has schema v{current}; this code supports up to v{SCHEMA_VERSION}")
    if current < SCHEMA_VERSION:
        _migrate(conn, path)
    return conn


def _enable_wal(conn: sqlite3.Connection, tries: int = 50, wait_s: float = 0.1) -> None:
    """Switching a fresh file to WAL needs an exclusive lock, and SQLite answers 'locked' at once instead of
    waiting for busy_timeout when another process is doing the same: retry."""
    for attempt in range(tries):
        try:
            conn.execute("pragma journal_mode=wal")
            return
        except sqlite3.OperationalError:
            if attempt == tries - 1:
                raise
            time.sleep(wait_s)


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("pragma user_version").fetchone()[0]


def _statements(script: str):
    """Split a DDL script into single statements (executescript would commit the lock we hold)."""
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            yield buf
            buf = ""


def _migrate(conn: sqlite3.Connection, path: Path) -> None:
    """Upgrade under the write lock. The version is read again once the lock is held: a second process that
    opened the same fresh file at the same moment has by then done the work, and repeating it would fail."""
    conn.execute("begin immediate")
    try:
        current = _user_version(conn)
        if current > SCHEMA_VERSION:
            raise SchemaError(f"{path} has schema v{current}; this code supports up to v{SCHEMA_VERSION}")
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for stmt in _statements(MIGRATIONS[version]):
                conn.execute(stmt)
            conn.execute(f"pragma user_version = {version}")
        conn.execute("commit")
    except BaseException:
        conn.execute("rollback")
        raise


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def start_run(conn: sqlite3.Connection, date: str, stage: str) -> int:
    return conn.execute(
        "insert into runs(date, stage, status, started) values (?, ?, 'running', ?)", (date, stage, _now())
    ).lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, error: str | None = None,
               meta: dict | None = None) -> None:
    if status not in RUN_STATUSES - {"running"}:
        raise ValueError(f"finish_run status must be one of {sorted(RUN_STATUSES - {'running'})}")
    conn.execute(
        "update runs set status=?, finished=?, error=?, meta=? where id=?",
        (status, _now(), error, json.dumps(meta) if meta is not None else None, run_id),
    )


def add_voice_cost(conn: sqlite3.Connection, date: str, segment: str, engine: str, chars: int, requests: int,
                   billed_usd: float, missing: int) -> None:
    conn.execute("insert into voice_costs(date, segment, engine, chars, requests, billed_usd, missing, at) "
                 "values (?, ?, ?, ?, ?, ?, ?, ?)", (date, segment, engine, chars, requests, billed_usd, missing, _now()))


def spend_today(conn: sqlite3.Connection, date: str) -> float:
    return conn.execute("select coalesce(sum(billed_usd), 0) from voice_costs where date=?", (date,)).fetchone()[0]
