import sqlite3

import pytest

from daily_brief import db

EXPECTED_TABLES = {"sources", "items", "transcripts", "ideas", "shows", "rundowns",
                   "rundown_items", "ledger", "feedback", "runs", "voice_costs"}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "brief.db")
    yield c
    c.close()


def tables(c):
    return {r[0] for r in c.execute("select name from sqlite_master where type='table'")}


def test_creates_all_tables_and_sets_version(conn):
    assert EXPECTED_TABLES <= tables(conn)
    assert conn.execute("pragma user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_wal_and_foreign_keys_on(conn):
    assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
    assert conn.execute("pragma foreign_keys").fetchone()[0] == 1


def test_connect_is_idempotent(tmp_path):
    p = tmp_path / "brief.db"
    db.connect(p).close()
    c = db.connect(p)  # second open must not fail or re-create
    assert c.execute("pragma user_version").fetchone()[0] == db.SCHEMA_VERSION
    c.close()


def test_refuses_newer_schema(tmp_path):
    p = tmp_path / "brief.db"
    c = sqlite3.connect(p)
    c.execute(f"pragma user_version = {db.SCHEMA_VERSION + 1}")
    c.close()
    with pytest.raises(db.SchemaError):
        db.connect(p)


def add_source(conn):
    return conn.execute(
        "insert into sources(segment, kind, url, name) values ('B','rss','https://x/feed','Lenny')"
    ).lastrowid


def test_item_guid_unique_per_source(conn):
    sid = add_source(conn)
    ins = "insert into items(source_id, guid, title) values (?, 'g1', 't')"
    conn.execute(ins, (sid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, (sid,))


def test_item_requires_existing_source(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into items(source_id, guid, title) values (999, 'g', 't')")


@pytest.mark.parametrize("table,col,bad", [
    ("sources", "segment", "Z"), ("sources", "kind", "ftp"),
])
def test_source_enums_enforced(conn, table, col, bad):
    vals = {"segment": "B", "kind": "rss"} | {col: bad}
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into sources(segment, kind, url, name) values (?, ?, 'u', 'n')",
                     (vals["segment"], vals["kind"]))


def test_item_status_enum_enforced(conn):
    sid = add_source(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into items(source_id, guid, title, status) values (?, 'g', 't', 'bogus')", (sid,))


def test_feedback_kind_enum_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into feedback(date, segment, cluster_id, kind) values ('2026-09-27','B','c1','love')")


def test_record_run_roundtrip(conn):
    rid = db.start_run(conn, "2026-09-27", "alarm-cache")
    db.finish_run(conn, rid, "ok", meta={"tracks": 12})
    row = conn.execute("select stage, status, finished, meta from runs where id=?", (rid,)).fetchone()
    assert row[0] == "alarm-cache" and row[1] == "ok" and row[2] is not None and '"tracks": 12' in row[3]


def test_finish_run_rejects_unknown_status(conn):
    rid = db.start_run(conn, "2026-09-27", "alarm-cache")
    with pytest.raises(ValueError):
        db.finish_run(conn, rid, "maybe")


def test_v2_database_upgrades_to_store_idea_parts(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript(db.SCHEMA_V1 + db.SCHEMA_V2 + "pragma user_version = 2;")
    c.close()
    conn = db.connect(p)
    assert "parts" in [r[1] for r in conn.execute("pragma table_info(ideas)")]
    assert conn.execute("pragma user_version").fetchone()[0] == db.SCHEMA_VERSION >= 3
    conn.close()


def test_spend_today_sums_billed_cost_for_that_date_only(conn):
    db.add_voice_cost(conn, "2026-10-26", "A", "heygen", 1000, 8, 0.004, 0)
    db.add_voice_cost(conn, "2026-10-26", "B", "heygen", 500, 4, 0.002, 1)
    db.add_voice_cost(conn, "2026-10-25", "A", "heygen", 9999, 9, 0.5, 0)
    assert db.spend_today(conn, "2026-10-26") == pytest.approx(0.006)
    assert db.spend_today(conn, "2026-10-27") == 0


def test_v5_adds_episodes_and_keeps_existing_rows(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path, isolation_level=None)  # a v4 database
    raw.executescript(db.SCHEMA_V1 + db.SCHEMA_V2 + db.SCHEMA_V3 + db.SCHEMA_V4
                      + "insert into sources(id,segment,kind,url,name) values (1,'A','youtube','u','n');"
                        "insert into items(id,source_id,guid,title) values (7,1,'g','t');"
                        "pragma user_version = 4;")
    raw.close()
    c = db.connect(path)
    assert c.execute("pragma user_version").fetchone()[0] == db.SCHEMA_VERSION >= 5
    assert c.execute("select id from items").fetchall() == [(7,)]
    c.execute("insert into episodes(item_id, segment, status, built_at) values (7, 'A', 'ready', 'now')")
    assert c.execute("select assigned_date from episodes").fetchone() == (None,)


def test_episodes_status_is_checked_and_needs_an_item(conn):
    conn.execute("insert into sources(id,segment,kind,url,name) values (1,'A','rss','https://u','n')")
    conn.execute("insert into items(id,source_id,guid,title) values (7,1,'g','t')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into episodes(item_id, segment, status, built_at) values (7, 'A', 'stale', 'now')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into episodes(item_id, segment, status, built_at) values (99, 'A', 'ready', 'now')")
    conn.execute("insert into episodes(item_id, segment, status, built_at) values (7, 'A', 'skipped', 'now')")
    with pytest.raises(sqlite3.IntegrityError):  # one episode per item
        conn.execute("insert into episodes(item_id, segment, status, built_at) values (7, 'A', 'ready', 'now')")


def test_episodes_queue_index_exists(conn):
    names = {r[1] for r in conn.execute("pragma index_list(episodes)")}
    assert "episodes_queue" in names


def test_v5_backfills_skipped_rows_for_items_the_old_night_already_aired(tmp_path):
    path = tmp_path / "v3.db"
    raw = sqlite3.connect(path, isolation_level=None)  # the v3 schema of 93a4828 (last release before v2)
    raw.executescript(
        db.SCHEMA_V1 + db.SCHEMA_V2 + db.SCHEMA_V3
        + "insert into sources(id,segment,kind,url,name) values (1,'A','rss','u1','n1'),(2,'B','rss','u2','n2');"
          "insert into items(id,source_id,guid,title,published_at,status) values"
          " (10,1,'g10','has ideas','2026-10-01','extracted'),"
          " (11,2,'g11','in a rundown','2026-10-02','transcribed'),"
          " (12,1,'g12','untouched','2026-10-03','discovered'),"
          " (13,2,'g13','near miss id','2026-10-03','transcribed');"
          "insert into ideas(item_id,local_id,claim) values (10,'i1','c');"
          "insert into rundown_items(date,segment,cluster_id,idea_ids,kept) values"
          " ('2026-10-04','B','c1','[\"e11.3\", \"e11.4\"]',1);"
          "pragma user_version = 3;")
    raw.close()
    c = db.connect(path)
    rows = c.execute("select item_id, segment, status, why, built_at, assigned_date from episodes order by item_id").fetchall()
    assert [r[:4] for r in rows] == [(10, "A", "skipped", "aired by v1"), (11, "B", "skipped", "aired by v1")]
    assert all(r[4] and r[5] is None for r in rows)
    # opening it again changes nothing
    c.close()
    assert db.connect(path).execute("select count(*) from episodes").fetchone() == (2,)


def test_the_user_version_is_reread_inside_the_write_lock(tmp_path, monkeypatch):
    path = tmp_path / "race.db"
    db.connect(path).close()  # another process finished migrating while we were about to
    real, calls = db._user_version, []

    def stale_first(conn):
        calls.append(1)
        return 0 if len(calls) == 1 else real(conn)

    monkeypatch.setattr(db, "_user_version", stale_first)
    c = db.connect(path)  # must not try "create table sources" again
    assert len(calls) >= 2 and real(c) == db.SCHEMA_VERSION


def test_two_connections_opening_a_fresh_database_both_succeed(tmp_path):
    import threading
    errors = []

    def open_it():
        try:
            db.connect(tmp_path / "fresh.db").close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=open_it) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []


def test_v6_lets_transcripts_record_asr_and_keeps_old_rows(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path, isolation_level=None)  # a v5 database with a whisper transcript, as an upgraded install has it
    raw.executescript(db.SCHEMA_V1 + db.SCHEMA_V2 + db.SCHEMA_V3 + db.SCHEMA_V4 + db.SCHEMA_V5
                      + "insert into sources(id,segment,kind,url,name) values (1,'A','youtube','u','n');"
                        "insert into items(id,source_id,guid,title) values (7,1,'g','t'), (8,1,'h','u'), (9,1,'i','v');"
                        "insert into transcripts(item_id,method,path,words,created_at) values (7,'whisper','p',5,'then');"
                        "pragma user_version = 5;")
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("insert into transcripts(item_id,method,path,words) values (8,'asr','p',1)")  # not yet allowed
    raw.close()
    c = db.connect(path)
    assert c.execute("pragma user_version").fetchone()[0] == db.SCHEMA_VERSION == 6
    assert c.execute("select item_id, method, path, words, created_at from transcripts").fetchall() \
        == [(7, "whisper", "p", 5, "then")]
    c.execute("insert into transcripts(item_id,method,path,words) values (8,'asr','p',1)")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("insert into transcripts(item_id,method,path,words) values (9,'vosk','p',1)")
    assert c.execute("pragma foreign_key_check").fetchall() == []
