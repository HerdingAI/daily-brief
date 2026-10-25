from pathlib import Path
from types import SimpleNamespace

import pytest

from daily_brief import db, transcribe


def seg(start, text):
    return SimpleNamespace(start=start, text=text)


def test_format_transcript_markers():
    out = transcribe.format_transcript([seg(0, " Hello."), seg(10, "World."), seg(31, "Next."),
                                        seg(3605, "Late.")], every_s=30)
    assert out.splitlines() == ["[00:00] Hello. World.", "[00:31] Next.", "[60:05] Late."]


@pytest.fixture
def setup(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    sid = conn.execute("insert into sources(segment,kind,url,name) values ('B','rss','https://f','Lenny')").lastrowid
    iid = conn.execute("insert into items(source_id,guid,title,audio_url) values (?,?,?,?)",
                       (sid, "g1", "Ep", "https://a/ep.mp3")).lastrowid
    return conn, iid, tmp_path


def fake_download(url, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"mp3")


def test_transcribe_item_success(setup):
    conn, iid, tmp = setup
    res = transcribe.transcribe_items(conn, [iid], tmp / "work", tmp / "transcripts", download=fake_download,
                                      whisper=lambda p: [seg(0, "hi there")], every_s=30, max_attempts=3)
    assert res == {"ok": 1, "failed": 0, "skipped": 0, "deferred": 0}
    status, = conn.execute("select status from items where id=?", (iid,)).fetchone()
    method, path, words = conn.execute("select method, path, words from transcripts where item_id=?",
                                       (iid,)).fetchone()
    assert status == "transcribed" and method == "whisper" and words == 2
    assert Path(path).read_text().startswith("[00:00] hi there")
    assert not list((tmp / "work").rglob("*.mp3"))  # audio deleted after use


def test_already_transcribed_is_skipped(setup):
    conn, iid, tmp = setup
    kw = {"download": fake_download, "whisper": lambda p: [seg(0, "x")], "every_s": 30, "max_attempts": 3}
    transcribe.transcribe_items(conn, [iid], tmp / "w", tmp / "t", **kw)
    assert transcribe.transcribe_items(conn, [iid], tmp / "w", tmp / "t", **kw)["skipped"] == 1


def test_failure_counts_attempts_then_marks_failed(setup):
    conn, iid, tmp = setup

    def broken(url, dest):
        raise OSError("403 forbidden")

    kw = {"download": broken, "whisper": lambda p: [], "every_s": 30, "max_attempts": 2}
    r1 = transcribe.transcribe_items(conn, [iid], tmp / "w", tmp / "t", **kw)
    row = conn.execute("select status, attempts, fail_reason from items where id=?", (iid,)).fetchone()
    assert r1["failed"] == 1 and row[0] == "discovered" and row[1] == 1 and "403" in row[2]
    transcribe.transcribe_items(conn, [iid], tmp / "w", tmp / "t", **kw)
    assert conn.execute("select status from items where id=?", (iid,)).fetchone()[0] == "failed"


def test_empty_transcript_is_a_failure(setup):
    conn, iid, tmp = setup
    r = transcribe.transcribe_items(conn, [iid], tmp / "w", tmp / "t", download=fake_download,
                                    whisper=lambda p: [], every_s=30, max_attempts=3)
    assert r["failed"] == 1
    assert conn.execute("select count(*) from transcripts").fetchone()[0] == 0
