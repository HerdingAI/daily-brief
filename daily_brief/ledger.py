"""Covered-topics ledger + listener feedback.

Each kept rundown item carries a short topic label from the editor. The ledger remembers when
each topic was covered; the editor's next run sees recent coverage and the listener's reactions
(👍 / go deeper / skip topic), so the show neither repeats itself nor ignores what they asked for.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3


def topic_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def record_covered(conn: sqlite3.Connection, date: str, rundown: dict) -> int:
    n = 0
    for x in rundown["rundown"]:
        key = topic_key(x.get("topic") or "")
        if not key:
            continue
        # last_covered < date guard: re-running the same night must not count the topic twice
        conn.execute(
            "insert into ledger(topic_key, first_covered, last_covered, times) values (?, ?, ?, 1) "
            "on conflict(topic_key) do update set times = times + (excluded.last_covered > ledger.last_covered), "
            "last_covered = max(ledger.last_covered, excluded.last_covered)", (key, date, date))
        n += 1
    return n


def editor_context(conn: sqlite3.Connection, today: str, days: int) -> str:
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat()
    covered = [{"topic": k, "last_covered": last, "times": t} for k, last, t in conn.execute(
        "select topic_key, last_covered, times from ledger where last_covered >= ? order by last_covered desc",
        (since,))]
    fb: dict[str, list[str]] = {"more_of": [], "go_deeper": [], "skip_topics": []}
    bucket = {"up": "more_of", "deeper": "go_deeper", "skip_topic": "skip_topics"}
    for kind, topic in conn.execute(
            "select f.kind, ri.topic from feedback f join rundown_items ri on ri.date = f.date "
            "and ri.segment = f.segment and ri.cluster_id = f.cluster_id "
            "where f.date >= ? and ri.topic is not null order by f.id", (since,)):
        key = topic_key(topic)
        if key not in fb[bucket[kind]]:
            fb[bucket[kind]].append(key)
    return json.dumps({"covered_recently": covered, "listener_feedback": fb}, indent=1)
