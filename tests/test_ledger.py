import json
import sqlite3

import pytest

from daily_brief import db, ledger


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "b.db")


RUNDOWN = {"rundown": [
    {"position": 1, "topic": "eval-the-trace", "cluster_ids": ["e1.i01"]},
    {"position": 2, "topic": "Writing As Thinking", "cluster_ids": ["e1.i02"]},
]}


def test_schema_v2_adds_topic_column(conn):
    cols = [r[1] for r in conn.execute("pragma table_info(rundown_items)")]
    assert "topic" in cols and conn.execute("pragma user_version").fetchone()[0] >= 2


def test_v1_database_is_migrated(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript(db.SCHEMA_V1 + "\npragma user_version = 1;")
    c.close()
    c = db.connect(p)
    assert "topic" in [r[1] for r in c.execute("pragma table_info(rundown_items)")]


def test_topic_keys_normalised():
    assert ledger.topic_key("  Writing As Thinking ") == "writing-as-thinking"
    assert ledger.topic_key("eval/the trace!") == "eval-the-trace"


def test_record_covered_upserts(conn):
    ledger.record_covered(conn, "2026-09-26", RUNDOWN)
    ledger.record_covered(conn, "2026-09-28", {"rundown": [{"topic": "eval-the-trace", "cluster_ids": []}]})
    rows = {r[0]: r[1:] for r in conn.execute("select topic_key, first_covered, last_covered, times from ledger")}
    assert rows["eval-the-trace"] == ("2026-09-26", "2026-09-28", 2)
    assert rows["writing-as-thinking"] == ("2026-09-26", "2026-09-26", 1)


def test_record_covered_is_idempotent_per_day(conn):
    ledger.record_covered(conn, "2026-09-26", RUNDOWN)
    ledger.record_covered(conn, "2026-09-26", RUNDOWN)  # a re-run of the same night
    assert conn.execute("select times from ledger where topic_key='eval-the-trace'").fetchone()[0] == 1


def test_editor_context_includes_recent_ledger_and_feedback(conn):
    ledger.record_covered(conn, "2026-09-20", RUNDOWN)
    conn.execute("insert into rundown_items(date,segment,cluster_id,idea_ids,kept,topic) values "
                 "('2026-09-20','B','k1','[]',1,'eval-the-trace'),('2026-09-20','B','k2','[]',1,'writing-as-thinking')")
    conn.execute("insert into feedback(date,segment,cluster_id,kind) values "
                 "('2026-09-20','B','k1','deeper'),('2026-09-20','B','k2','skip_topic')")
    ctx = json.loads(ledger.editor_context(conn, today="2026-09-27", days=30))
    assert {c["topic"] for c in ctx["covered_recently"]} == {"eval-the-trace", "writing-as-thinking"}
    assert ctx["listener_feedback"]["go_deeper"] == ["eval-the-trace"]
    assert ctx["listener_feedback"]["skip_topics"] == ["writing-as-thinking"]


def test_old_ledger_entries_fall_out_of_context(conn):
    ledger.record_covered(conn, "2026-06-01", RUNDOWN)
    ctx = json.loads(ledger.editor_context(conn, today="2026-09-27", days=30))
    assert ctx["covered_recently"] == []
