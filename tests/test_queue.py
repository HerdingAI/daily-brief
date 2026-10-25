import json
import shutil
import subprocess
from dataclasses import replace

import pytest

from daily_brief import db, ledger, mix, queue
from daily_brief.config import load_config
from tests.conftest import write_config

DATE = "2026-10-26"


@pytest.fixture
def env(tmp_path):
    cfg = load_config(write_config(tmp_path))
    return cfg, db.connect(cfg.paths.var_dir / "brief.db")


def fake_concat(paths, out):
    out.write_bytes(b"|".join(p.read_bytes() for p in paths))


def probe_sum(p):
    return 600.0


def add_episode(conn, cfg, item_id, segment, seconds=600.0, published="2026-10-20", backcat=0, status="ready",
                chapters=None, title=None, source="Lenny"):
    """A source (one per name), an item, an episodes row and (for ready ones) a library file."""
    conn.execute("insert or ignore into sources(segment, kind, url, name) values (?, 'rss', ?, ?)",
                 (segment, f"https://x.test/{source}/{segment}", source))
    sid = conn.execute("select id from sources where url = ?", (f"https://x.test/{source}/{segment}",)).fetchone()[0]
    conn.execute("insert into items(id, source_id, guid, title, published_at, is_backcatalog, status) "
                 "values (?, ?, ?, ?, ?, ?, 'transcribed')", (item_id, sid, f"g{item_id}", f"src {item_id}", published, backcat))
    path = None
    if status == "ready":
        path = cfg.paths.var_dir / "library" / str(item_id) / "episode.mp3"
        path.parent.mkdir(parents=True)
        path.write_bytes(f"ep{item_id}".encode())
    chapters = chapters if chapters is not None else [
        {"topic": f"topic {item_id} a", "why": f"why {item_id} a", "priority": "p1"},
        {"topic": f"topic {item_id} b", "why": f"why {item_id} b", "priority": "p2"}]
    conn.execute("insert into episodes(item_id, segment, status, title, why, chapters, so_what, path, seconds, "
                 "built_at) values (?,?,?,?,?,?,?,?,?,?)",
                 (item_id, segment, status, title or f"Episode {item_id}", f"episode why {item_id}",
                  json.dumps(chapters), f"so what {item_id}", str(path) if path else None, seconds,
                  f"2026-10-25T0{item_id % 10}:00:00-05:00"))


def assemble(conn, cfg, date=DATE, **kw):
    return queue.assemble(conn, cfg, date, concat=kw.get("concat", fake_concat), probe=kw.get("probe", probe_sum))


def assigned(conn):
    return dict(conn.execute("select item_id, assigned_date from episodes").fetchall())


def test_assemble_idempotent_and_assigns_once(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A")
    add_episode(conn, cfg, 2, "B")
    first = assemble(conn, cfg)
    assert first["segments"] == {"A": "A.mp3", "B": "B.mp3"}
    assert first["meta"]["A"]["items"] == [1] and first["meta"]["B"]["items"] == [2]
    assert {"title", "seconds", "items", "ready_at"} <= set(first["meta"]["A"])
    assert assigned(conn) == {1: DATE, 2: DATE}
    add_episode(conn, cfg, 3, "A")  # built after the morning was assembled
    again = assemble(conn, cfg, concat=lambda *a: pytest.fail("a second call must change nothing"))
    assert again == first and assigned(conn)[3] is None
    # the next date takes only what is still waiting: an episode airs on exactly one date
    nxt = assemble(conn, cfg, "2026-10-27")
    assert nxt["meta"]["A"]["items"] == [3] and "B" not in nxt["segments"]
    assert assigned(conn) == {1: DATE, 2: DATE, 3: "2026-10-27"}


def test_assemble_caps_45_min_per_segment(env):
    cfg, conn = env
    for n, published in ((1, "2026-10-24"), (2, "2026-10-23"), (3, "2026-10-22"), (4, "2026-10-21")):
        add_episode(conn, cfg, n, "A", seconds=20 * 60, published=published)  # 20 min each: two fit in 45
    ready = assemble(conn, cfg)
    assert ready["meta"]["A"]["items"] == [1, 2]       # newest first, the third would make 60 min
    assert assigned(conn)[3] is None and assigned(conn)[4] is None


def test_assemble_skips_an_episode_that_does_not_fit_but_takes_a_shorter_older_one(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A", seconds=30 * 60, published="2026-10-24")
    add_episode(conn, cfg, 2, "A", seconds=25 * 60, published="2026-10-23")   # 55 > 45: passed over
    add_episode(conn, cfg, 3, "A", seconds=10 * 60, published="2026-10-22")
    assert assemble(conn, cfg)["meta"]["A"]["items"] == [1, 3]


def test_assemble_new_before_backcatalog(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "B", seconds=20 * 60, published="2026-10-24", backcat=1)   # newer, but old content
    add_episode(conn, cfg, 2, "B", seconds=20 * 60, published="2026-10-20")
    add_episode(conn, cfg, 3, "B", seconds=20 * 60, published="2026-10-21")
    assert assemble(conn, cfg)["meta"]["B"]["items"] == [3, 2]    # new ones, newest first; the back catalog waits


def test_assemble_nothing_ready_writes_no_ready(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A", status="skipped")
    assert assemble(conn, cfg) == {}
    assert not (cfg.paths.var_dir / "show" / DATE / "READY").exists()
    assert conn.execute("select count(*) from rundowns").fetchone()[0] == 0
    assert not conn.in_transaction


def test_episode_with_a_missing_file_is_never_taken(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A")
    (cfg.paths.var_dir / "library" / "1" / "episode.mp3").unlink()
    assert assemble(conn, cfg) == {} and assigned(conn) == {1: None}


def test_crash_before_ready_leaves_episodes_unassigned(env, monkeypatch):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A")
    add_episode(conn, cfg, 2, "B")

    def boom(show_dir, data):
        raise OSError("disk full")

    monkeypatch.setattr(queue, "_write_ready", boom)
    with pytest.raises(OSError):
        assemble(conn, cfg)
    assert assigned(conn) == {1: None, 2: None}
    assert conn.execute("select count(*) from rundowns").fetchone()[0] == 0
    assert conn.execute("select count(*) from rundown_items").fetchone()[0] == 0
    assert conn.execute("select count(*) from ledger").fetchone()[0] == 0
    assert not (cfg.paths.var_dir / "show" / DATE / "READY").exists() and not conn.in_transaction
    monkeypatch.undo()
    assert assemble(conn, cfg)["segments"] == {"A": "A.mp3", "B": "B.mp3"}   # the retry works


def test_a_segment_that_cannot_be_cut_does_not_stop_the_other_one(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A")
    add_episode(conn, cfg, 2, "B")

    def concat(paths, out):
        if out.name.startswith("B."):
            raise queue.QueueError("ffmpeg concat failed")
        fake_concat(paths, out)

    ready = assemble(conn, cfg, concat=concat)
    assert ready["segments"] == {"A": "A.mp3"} and ready["meta"]["A"]["items"] == [1]
    # the file that breaks even on its own, while ffmpeg works for the other segment, is set aside
    assert assigned(conn) == {1: DATE, 2: None}
    assert conn.execute("select status from episodes where item_id = 2").fetchone() == ("bad",)
    assert conn.execute("select count(*) from rundowns where segment = 'B'").fetchone() == (0,)


def test_one_corrupt_file_in_a_segment_is_dropped_and_the_rest_is_cut(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A", published="2026-10-24")
    add_episode(conn, cfg, 2, "A", published="2026-10-23", source="Other")
    add_episode(conn, cfg, 3, "A", published="2026-10-22", source="Third")

    def concat(paths, out):
        if any(p.read_bytes() == b"ep2" for p in paths):
            raise queue.QueueError("ffmpeg concat failed")
        fake_concat(paths, out)

    ready = assemble(conn, cfg, concat=concat)
    assert ready["meta"]["A"]["items"] == [1, 3]
    assert assigned(conn) == {1: DATE, 2: None, 3: DATE}
    assert conn.execute("select status from episodes where item_id = 2").fetchone() == ("bad",)
    assert [r[0] for r in conn.execute("select json_extract(idea_ids, '$[0]') from rundown_items order by position")] \
        == [1, 1, 3, 3]


def test_when_nothing_can_be_cut_the_environment_is_blamed_not_the_episodes(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A")
    add_episode(conn, cfg, 2, "B")

    def no_ffmpeg(paths, out):
        raise queue.QueueError("ffmpeg concat failed")

    with pytest.raises(queue.QueueError):
        assemble(conn, cfg, concat=no_ffmpeg)
    assert assigned(conn) == {1: None, 2: None}
    assert conn.execute("select count(*) from episodes where status = 'bad'").fetchone() == (0,)
    assert not (cfg.paths.var_dir / "show" / DATE / "READY").exists() and not conn.in_transaction


def test_a_corrupt_library_file_is_skipped_marked_bad_and_never_probed_again(env):
    cfg, conn = env
    for n, src in ((1, "Lenny"), (2, "Other"), (3, "Third")):
        add_episode(conn, cfg, n, "A", source=src)
    probed = []

    def probe(p):
        probed.append(p.parent.name)
        return None if p.parent.name == "2" else 600.0

    ready = assemble(conn, cfg, probe=probe)
    assert ready["meta"]["A"]["items"] == [3, 1]
    assert conn.execute("select status from episodes where item_id = 2").fetchone() == ("bad",)
    assert "unplayable" in conn.execute("select why from episodes where item_id = 2").fetchone()[0]
    add_episode(conn, cfg, 4, "A", source="Fourth")
    probed.clear()
    assemble(conn, cfg, "2026-10-27", probe=probe)
    assert "2" not in probed


def test_a_too_short_file_counts_as_corrupt(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A")
    short = cfg.player.min_segment_seconds - 1
    assert assemble(conn, cfg, probe=lambda p: short) == {}
    assert conn.execute("select status from episodes").fetchone() == ("bad",)


def test_a_probe_that_cannot_run_does_not_condemn_episodes(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A")

    def no_ffprobe(p):
        raise FileNotFoundError("ffprobe")

    assert queue.plan(conn, cfg, probe=no_ffprobe) == {}
    assert conn.execute("select status from episodes").fetchone() == ("ready",)


def test_failed_commit_removes_the_ready_it_just_wrote(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A")

    class Conn:   # delegates to the real connection, but the commit fails
        def execute(self, sql, *a):
            if sql == "commit":
                raise db.sqlite3.OperationalError("disk I/O error")
            return conn.execute(sql, *a)

    with pytest.raises(db.sqlite3.OperationalError):
        assemble(Conn(), cfg)
    assert not (cfg.paths.var_dir / "show" / DATE / "READY").exists()
    assert assigned(conn) == {1: None}


def test_ready_is_written_atomically_and_last(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A")
    seen = {}

    def concat(paths, out):
        seen["ready_during_cut"] = (out.parent / "READY").exists()
        fake_concat(paths, out)

    assemble(conn, cfg, concat=concat)
    d = cfg.paths.var_dir / "show" / DATE
    assert seen["ready_during_cut"] is False and (d / "READY").exists() and not (d / "READY.tmp").exists()


def test_rundown_has_one_item_per_chapter_in_play_order(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A", seconds=600, published="2026-10-24", title="First", source="AI Engineer")
    add_episode(conn, cfg, 2, "A", seconds=300, published="2026-10-23", title="Second", source="AI Engineer",
                chapters=[{"topic": "solo", "why": "only chapter", "priority": "p3"}])
    assemble(conn, cfg)
    rd = json.loads(conn.execute("select json from rundowns where date = ? and segment = 'A'", (DATE,)).fetchone()[0])
    assert rd["segment_title"] == "First (+1 more)" and rd["cut"] == []
    assert rd["editor_note"] == "episode why 1 episode why 2"
    assert rd["so_what"] == {"text": "so what 1 so what 2", "projects": []}
    got = [(x["position"], x["role"], x["topic"], x["priority"], x["why_kept"], x["minutes"], x["sources"])
           for x in rd["rundown"]]
    assert got == [(1, "headline", "topic 1 a", "p1", "why 1 a", 5.0, ["AI Engineer"]),
                   (2, "deep_dive", "topic 1 b", "p2", "why 1 b", 5.0, ["AI Engineer"]),
                   (3, "deep_dive", "solo", "p3", "only chapter", 5.0, ["AI Engineer"])]
    rows = conn.execute("select cluster_id, idea_ids, kept, reason, topic, position from rundown_items "
                        "where date = ? and segment = 'A' order by position", (DATE,)).fetchall()
    assert rows == [("k1", "[1]", 1, "why 1 a", "topic 1 a", 1), ("k2", "[1]", 1, "why 1 b", "topic 1 b", 2),
                    ("k3", "[2]", 1, "only chapter", "solo", 3)]


def test_single_episode_title_is_its_own(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "B", title="Solo title")
    assert assemble(conn, cfg)["meta"]["B"]["title"] == "Solo title"


def test_covered_topics_are_recorded_once(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A")
    assemble(conn, cfg)
    assert {r[0] for r in conn.execute("select topic_key from ledger")} == {ledger.topic_key("topic 1 a"),
                                                                          ledger.topic_key("topic 1 b")}
    assemble(conn, cfg)  # idempotent: no double count
    assert conn.execute("select max(times) from ledger").fetchone()[0] == 1


def test_assembled_day_serves_feedback_today_and_library(env):
    from fastapi.testclient import TestClient

    from daily_brief import api
    cfg, conn = env
    add_episode(conn, cfg, 1, "A")
    assemble(conn, cfg)
    c = TestClient(api.create_app(cfg, today=lambda: DATE, launcher=lambda p: None))
    seg = c.get("/today").json()["segments"]["A"]
    assert [k["cluster_id"] for k in seg["kept"]] == ["k1", "k2"] and seg["kept"][0]["role"] == "headline"
    assert seg["title"] == "Episode 1" and seg["so_what"] == "so what 1" and seg["cut"] == []
    assert c.post("/feedback", json={"date": DATE, "segment": "A", "cluster_id": "k2", "kind": "deeper"}).status_code == 200
    assert c.post("/feedback", json={"date": DATE, "segment": "A", "cluster_id": "k9", "kind": "up"}).status_code == 404
    exp = c.get("/export", params={"since": DATE}).json()["feedback"][0]
    assert exp["topic"] == "topic 1 b" and exp["why"] == "why 1 b"
    lib = c.get("/library").json()["days"][0]
    assert lib["date"] == DATE and lib["segments"][0]["title"] == "Episode 1" and lib["segments"][0]["seconds"] == 600.0


def test_show_cap_is_configurable(env):
    cfg, conn = env
    cfg = replace(cfg, show=replace(cfg.show, max_segment_minutes=25))
    for n in (1, 2):
        add_episode(conn, cfg, n, "A", seconds=20 * 60, published=f"2026-10-2{5 - n}")
    assert assemble(conn, cfg)["meta"]["A"]["items"] == [1]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_real_concat_joins_without_reencoding(tmp_path):
    parts = []
    for i, secs in enumerate((2, 3)):
        p = tmp_path / f"it's {i}.mp3"   # a quote in the name exercises the demuxer escaping
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency={440 + i * 220}:duration={secs}:sample_rate=44100", "-b:a", "128k", str(p)],
                       check=True)
        parts.append(p)
    out = tmp_path / "show" / "A.mp3"
    out.parent.mkdir()
    queue.concat_mp3(parts, out)
    assert abs(mix.probe_duration(out) - 5) < 0.3
    assert sorted(x.name for x in out.parent.iterdir()) == ["A.mp3"]    # no list or tmp file left behind


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_concat_failure_raises_and_leaves_nothing(tmp_path):
    bad = tmp_path / "not-audio.mp3"
    bad.write_bytes(b"nope")
    with pytest.raises(queue.QueueError):
        queue.concat_mp3([bad], tmp_path / "A.mp3")
    assert [p.name for p in tmp_path.iterdir()] == ["not-audio.mp3"]


def _crash_after_ready(conn, items=(1, 2)):
    """The state a power cut leaves between writing READY and the commit: READY on disk, nothing in the DB."""
    conn.execute(f"update episodes set assigned_date = null where item_id in ({','.join(map(str, items))})")
    conn.execute("delete from rundowns")
    conn.execute("delete from rundown_items")
    conn.execute("delete from ledger")


def test_ready_over_unassigned_episodes_is_completed_not_aired_twice(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A")
    add_episode(conn, cfg, 2, "B")
    first = assemble(conn, cfg)
    _crash_after_ready(conn)
    add_episode(conn, cfg, 3, "A", source="Other")      # built since: must wait for tomorrow
    again = assemble(conn, cfg, concat=lambda *a: pytest.fail("the files already exist"))
    assert again == first
    assert assigned(conn) == {1: DATE, 2: DATE, 3: None}
    assert conn.execute("select segment, count(*) from rundown_items group by segment order by segment").fetchall() \
        == [("A", 2), ("B", 2)]
    assert conn.execute("select count(*) from rundowns").fetchone() == (2,)
    assert conn.execute("select max(times) from ledger").fetchone()[0] == 1
    assert not conn.in_transaction
    assert assemble(conn, cfg) == first and conn.execute("select max(times) from ledger").fetchone()[0] == 1


def test_completing_a_crashed_assembly_leaves_an_episode_of_another_date_alone(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A")
    add_episode(conn, cfg, 2, "A", source="Other")
    assemble(conn, cfg)
    _crash_after_ready(conn, (1,))
    conn.execute("update episodes set assigned_date = '2026-10-20' where item_id = 2")   # taken by someone else
    assemble(conn, cfg)
    assert assigned(conn) == {1: DATE, 2: "2026-10-20"}


def test_a_healthy_day_costs_no_write_on_repeat(env):
    cfg, conn = env
    add_episode(conn, cfg, 1, "A")
    first = assemble(conn, cfg)
    changes = conn.total_changes
    assert assemble(conn, cfg) == first and conn.total_changes == changes
