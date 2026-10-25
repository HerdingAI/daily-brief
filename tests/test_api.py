import datetime as dt
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from daily_brief import api, db, queue
from daily_brief.config import load_config
from tests.conftest import write_config

REPO = Path(__file__).resolve().parent.parent
DATE = "2026-09-27"
RUNDOWN = json.loads((Path(__file__).parent / "fixtures" / "example_rundown.json").read_text())



@pytest.fixture
def client(tmp_path):
    cfg = load_config(write_config(tmp_path))  # every runtime path redirected into tmp_path
    conn = db.connect(cfg.paths.var_dir / "brief.db")
    conn.execute("insert into rundowns(date,segment,json,brief_version) values (?,?,?,0)",
                 (DATE, "B", json.dumps(RUNDOWN)))
    for x in RUNDOWN["rundown"]:
        conn.execute("insert into rundown_items(date,segment,cluster_id,idea_ids,kept,position) values (?,?,?,?,1,?)",
                     (DATE, "B", f"k{x['position']}", json.dumps(x["cluster_ids"]), x["position"]))
    show = cfg.paths.var_dir / "show" / DATE
    show.mkdir(parents=True)
    (show / "READY").write_text(json.dumps({"segments": {"B": "B.mp3"}, "meta": {"B": {"seconds": 600}}}))
    (show / "B.mp3").write_bytes(b"x")
    app = api.create_app(cfg, today=lambda: DATE)
    app.state.probe = lambda p: 600.0
    return TestClient(app), conn


def test_health(client):
    c, _ = client
    r = c.get("/health").json()
    assert r["ok"] and r["date"] == DATE and r["ready_segments"] == ["B"]


def test_health_lists_enabled_segments_that_are_not_ready(client):
    c, _ = client
    r = c.get("/health").json()
    assert set(r["enabled_segments"]) >= {"B"}
    assert r["missing_segments"] == sorted(set(r["enabled_segments"]) - {"B"})


def test_today_has_kept_and_cut(client):
    c, _ = client
    r = c.get("/today").json()
    seg = r["segments"]["B"]
    assert seg["title"] == RUNDOWN["segment_title"]
    assert [k["cluster_id"] for k in seg["kept"]] == ["k1", "k2", "k3", "k4", "k5"]
    assert len(seg["cut"]) == len(RUNDOWN["cut"]) and seg["so_what"]


def test_feedback_recorded_and_validated(client):
    c, conn = client
    ok = c.post("/feedback", json={"date": DATE, "segment": "B", "cluster_id": "k2", "kind": "deeper", "note": "more"})
    assert ok.status_code == 200
    assert conn.execute("select cluster_id, kind, note from feedback").fetchall() == [("k2", "deeper", "more")]
    assert c.post("/feedback", json={"date": DATE, "segment": "B", "cluster_id": "k99", "kind": "up"}).status_code == 404
    assert c.post("/feedback", json={"date": DATE, "segment": "B", "cluster_id": "k1", "kind": "love"}).status_code == 422


def test_export_feedback_and_ledger(client):
    c, conn = client
    c.post("/feedback", json={"date": DATE, "segment": "B", "cluster_id": "k1", "kind": "up"})
    conn.execute("insert into ledger(topic_key, first_covered, last_covered) values ('evals','2026-09-27','2026-09-27')")
    conn.execute("update rundown_items set topic='eval the trace', reason='why k1' where cluster_id='k1'")
    r = c.get("/export", params={"since": "2026-09-01"}).json()
    assert r["feedback"][0]["kind"] == "up" and r["ledger"][0]["topic_key"] == "evals"
    assert r["feedback"][0]["topic"] == "eval the trace" and r["feedback"][0]["why"] == "why k1"


def test_phone_page_served(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "/today" in r.text


# ---- /today before assembly, /health/show, /health/llm ---------------------------------------
from tests.test_queue import add_episode


@pytest.fixture
def lib(tmp_path):
    """(cfg, client, db connection) with an empty library."""
    cfg = load_config(write_config(tmp_path))  # every runtime path redirected into tmp_path
    conn = db.connect(cfg.paths.var_dir / "brief.db")
    app = api.create_app(cfg, today=lambda: DATE)
    app.state.probe = lambda p: 600.0
    return cfg, TestClient(app), conn


def test_today_shows_up_next_before_assembly(lib):
    cfg, c, conn = lib
    add_episode(conn, cfg, 1, "A", seconds=600, title="Agents in prod")
    add_episode(conn, cfg, 2, "B", seconds=300, title="Pricing")
    add_episode(conn, cfg, 3, "B", status="skipped")
    r = c.get("/today").json()
    assert r["segments"] == {} and r["ready"] == {"segments": {}, "meta": {}}
    assert [(e["item_id"], e["segment"], e["title"], e["why"], e["seconds"]) for e in r["up_next"]] == [
        (1, "A", "Agents in prod", "episode why 1", 600.0), (2, "B", "Pricing", "episode why 2", 300.0)]
    assert set(r["up_next"][0]) == {"item_id", "segment", "title", "why", "seconds", "built_at"}
    # reading must not assemble
    assert not (cfg.paths.var_dir / "show" / DATE).exists()
    assert conn.execute("select count(*) from episodes where assigned_date is not null").fetchone()[0] == 0


def test_today_up_next_is_empty_once_assembled_and_for_other_days(client):
    c, _ = client      # the client fixture has an assembled DATE
    r = c.get("/today").json()
    assert r["up_next"] == [] and "B" in r["segments"]
    assert c.get("/today", params={"date": "2026-01-01"}).json()["up_next"] == []


def test_health_show_counts_ready_and_spend(lib):
    cfg, c, conn = lib
    add_episode(conn, cfg, 1, "A")
    add_episode(conn, cfg, 2, "A")
    add_episode(conn, cfg, 3, "B")
    conn.execute("update episodes set assigned_date = '2026-09-26' where item_id = 2")   # aired: not waiting
    add_episode(conn, cfg, 4, "B", status="skipped")
    db.add_voice_cost(conn, DATE, "A", "heygen", 1000, 3, 0.04, 0)
    db.add_voice_cost(conn, "2026-09-26", "A", "heygen", 1000, 3, 0.09, 0)    # yesterday's spend is not today's
    r = c.get("/health/show").json()
    assert r["ready_next"] == {"A": 1, "B": 1}
    assert r["spend_today"] == 0.04 and r["cap"] == 0.15
    import datetime as dt
    assert r["last_built_at"] == int(dt.datetime.fromisoformat("2026-10-25T03:00:00-05:00").timestamp())
    assert r["last_build"] == {"status": None, "error": None, "at": None, "outcome": None}
    assert r["failed_in_a_row"] == 0 and r["ready_today"] == {"A": False, "B": False}
    assert set(r) == {"ready_next", "ready_today", "last_built_at", "last_build", "failed_in_a_row", "feeds_failing",
                      "spend_today", "cap"}


def test_health_show_ready_today_follows_todays_ready_file(lib):
    cfg, c, conn = lib
    add_episode(conn, cfg, 1, "A")
    queue.assemble(conn, cfg, DATE, concat=lambda paths, out: out.write_bytes(b"x"), probe=lambda p: 600.0)
    r = c.get("/health/show").json()
    assert r["ready_today"] == {"A": True, "B": False} and r["ready_next"] == {"A": 0, "B": 0}
    (cfg.paths.var_dir / "show" / DATE / "READY").write_text("{not json")
    assert c.get("/health/show").json()["ready_today"] == {"A": False, "B": False}


def test_health_show_last_build_carries_the_outcome(lib):
    _, c, conn = lib
    run = db.start_run(conn, DATE, "build")
    db.finish_run(conn, run, "failed", error="capped: $0.14 spent", meta={"outcome": "capped"})
    assert c.get("/health/show").json()["last_build"]["outcome"] == "capped"
    run = db.start_run(conn, DATE, "build")
    db.finish_run(conn, run, "failed", error="LLMAuthError: out")   # no meta: no outcome
    assert c.get("/health/show").json()["last_build"]["outcome"] is None


def test_health_show_empty_library(lib):
    _, c, _ = lib
    r = c.get("/health/show").json()
    assert r["ready_next"] == {"A": 0, "B": 0} and r["last_built_at"] == 0 and r["spend_today"] == 0


def _build_run(conn, status, error=None):
    run = db.start_run(conn, DATE, "build")
    if status != "running":
        db.finish_run(conn, run, status, error=error)


def test_health_show_last_build_and_failures_in_a_row(lib):
    _, c, conn = lib
    _build_run(conn, "failed", "old")
    _build_run(conn, "ok")
    _build_run(conn, "failed", "LLMAuthError: logged out")
    _build_run(conn, "running")                       # in flight: skipped over when counting
    _build_run(conn, "failed", "RuntimeError: boom")
    r = c.get("/health/show").json()
    assert r["failed_in_a_row"] == 2
    assert r["last_build"]["status"] == "failed" and r["last_build"]["error"] == "RuntimeError: boom"
    assert r["last_build"]["at"]
    _build_run(conn, "ok")
    assert c.get("/health/show").json()["failed_in_a_row"] == 0
    conn.execute("insert into runs(date, stage, status, started) values (?, 'show', 'failed', 'x')", (DATE,))
    assert c.get("/health/show").json()["failed_in_a_row"] == 0   # other stages never count


def test_health_llm_is_cached_and_uses_the_builders_token(lib, tmp_path):
    cfg, c, _ = lib
    secrets_file = cfg.voice.secrets_file
    secrets_file.parent.mkdir(parents=True, exist_ok=True)
    secrets_file.write_text("OPENROUTER_API_KEY=or-secret\nCLAUDE_CODE_OAUTH_TOKEN=tok-secret\n")
    calls, now = [], [1000.0]

    def status(command, env):
        calls.append((command, env))
        return {"loggedIn": True, "authMethod": "oauth_token", "tokenRejected": False}

    c.app.state.auth_status = status
    c.app.state.clock = lambda: now[0]
    first = c.get("/health/llm")
    assert first.json() == {"loggedIn": True, "authMethod": "oauth_token", "tokenRejected": False}
    assert calls == [(str(cfg.llm.command), {"CLAUDE_CODE_OAUTH_TOKEN": "tok-secret"})]
    assert "tok-secret" not in first.text and "or-secret" not in first.text
    now[0] += cfg.llm.health_cache_seconds - 1
    c.get("/health/llm")
    assert len(calls) == 1                    # still cached
    now[0] += 2
    c.get("/health/llm")
    assert len(calls) == 2                    # expired


def test_health_llm_returns_only_login_state_never_account_details(lib):
    _, c, _ = lib
    c.app.state.clock = lambda: 1.0
    c.app.state.auth_status = lambda command, env: {
        "loggedIn": True, "authMethod": "claude.ai", "email": "me@example.com", "orgName": "Acme", "apiProvider": "x"}
    r = c.get("/health/llm")
    assert r.json() == {"loggedIn": True, "authMethod": "claude.ai", "tokenRejected": False}
    assert "me@example.com" not in r.text and "Acme" not in r.text
    assert c.get("/health/llm").json() == {"loggedIn": True, "authMethod": "claude.ai", "tokenRejected": False}  # the cached copy too


def test_health_llm_reports_logged_out_and_does_not_cache_errors(lib):
    from daily_brief import preflight
    _, c, _ = lib
    c.app.state.clock = lambda: 5.0
    c.app.state.auth_status = lambda command, env: {"loggedIn": False}
    assert c.get("/health/llm").json() == {"loggedIn": False, "authMethod": None, "tokenRejected": False}

    def unreadable(command, env):
        raise preflight.PreflightError("claude auth status unreadable: OSError")

    c.app.state.clock = lambda: 99999.0
    c.app.state.auth_status = unreadable
    r = c.get("/health/llm")
    assert r.status_code == 200 and r.json()["loggedIn"] is False and "unreadable" in r.json()["error"]
    c.app.state.auth_status = lambda command, env: {"loggedIn": True}
    assert c.get("/health/llm").json()["loggedIn"] is True     # the error was not cached


def test_phone_page_has_up_next_and_spend_line(client):
    c, _ = client
    html = c.get("/").text
    for needle in ("Up next", "up_next", "/health/show", "voice spend today", 'id="spend"', "latest.mp3"):
        assert needle in html


@pytest.fixture
def ondemand(tmp_path):
    cfg = load_config(write_config(tmp_path))  # every runtime path redirected into tmp_path
    db.connect(cfg.paths.var_dir / "brief.db")
    app = api.create_app(cfg, today=lambda: DATE)
    app.state.probe = lambda p: 600.0
    return cfg, TestClient(app)


# ---- library ------------------------------------------------------
def archived_show(root, date, segs, title="T"):
    d = root / date
    d.mkdir(parents=True, exist_ok=True)
    for s in segs:
        (d / f"{s}.mp3").write_bytes(b"x")
    (d / "READY").write_text(json.dumps({"segments": {s: f"{s}.mp3" for s in segs},
                                         "meta": {s: {"title": f"{title} {s}", "seconds": 600} for s in segs}}))


def test_library_merges_local_and_archive_newest_first(ondemand):
    cfg, c = ondemand
    archived_show(cfg.paths.var_dir / "show", DATE, ["A", "B"], "Today")
    archived_show(cfg.paths.archive_dir, "2026-09-20", ["B"], "Old")
    archived_show(cfg.paths.archive_dir, DATE, ["A", "B"], "Dup")  # same day in both: local wins
    days = c.get("/library").json()["days"]
    assert [d["date"] for d in days] == [DATE, "2026-09-20"]
    assert [s["segment"] for s in days[0]["segments"]] == ["A", "B"]
    assert days[0]["segments"][0]["title"] == "Today A" and days[1]["segments"][0]["title"] == "Old B"


def _llm_run(conn, error=None, meta=None):
    run = db.start_run(conn, DATE, "build")
    db.finish_run(conn, run, "failed" if error else "ok", error=error, meta=meta)


def test_health_llm_token_refused_by_the_last_build_that_reached_claude(lib):
    _, c, conn = lib
    c.app.state.clock = lambda: 1.0
    c.app.state.auth_status = lambda command, env: {"loggedIn": True, "authMethod": "oauth_token"}  # set != valid
    assert c.get("/health/llm").json()["tokenRejected"] is False   # no build yet: nothing decides
    _llm_run(conn, meta={"outcome": "ready", "llm_ok": True})
    _llm_run(conn, error="LLMAuthError: exit 1: Failed to authenticate. API Error: 401 Invalid bearer token sk-leak")
    r = c.get("/health/llm")
    assert r.json() == {"loggedIn": False, "authMethod": "oauth_token", "tokenRejected": True}
    assert "sk-leak" not in r.text and "401" not in r.text
    # runs that never called Claude don't decide: still rejected
    _llm_run(conn, meta={"outcome": "no candidates"})
    _llm_run(conn, error="capped: over", meta={"outcome": "capped", "results": []})
    _llm_run(conn, error="RuntimeError: boom")
    assert c.get("/health/llm").json()["loggedIn"] is False
    db.start_run(conn, DATE, "build")  # in flight
    assert c.get("/health/llm").json()["tokenRejected"] is True
    # the next build whose Claude call succeeds clears it, with the cached auth status untouched
    _llm_run(conn, meta={"outcome": "skipped", "llm_ok": True})
    assert c.get("/health/llm").json() == {"loggedIn": True, "authMethod": "oauth_token", "tokenRejected": False}


def test_health_llm_logged_out_by_auth_status_stays_logged_out_after_a_good_build(lib):
    _, c, conn = lib
    c.app.state.clock = lambda: 1.0
    c.app.state.auth_status = lambda command, env: {"loggedIn": False}
    _llm_run(conn, meta={"outcome": "ready", "llm_ok": True})
    assert c.get("/health/llm").json() == {"loggedIn": False, "authMethod": None, "tokenRejected": False}


# ---- /health/show feeds_failing ---------------------------------------------------------------

def _feed_run(conn, hours_ago, ok=(), failed=None, meta=True):
    """A finished build run `hours_ago` old; meta=False = an older row with no per-feed info."""
    started = (dt.datetime.now().astimezone() - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    m = json.dumps({"feeds_ok": list(ok), "feeds_failed": failed or {}}) if meta else json.dumps({"outcome": "ready"})
    conn.execute("insert into runs(date, stage, status, started, finished, meta) values (?, 'build', 'ok', ?, ?, ?)",
                 (DATE, started, started, m))
    return started


def test_feeds_failing_lists_a_feed_failing_for_longer_than_the_window(lib):
    cfg, c, conn = lib
    a, b = (s.name for s in cfg.sources[:2])
    first = _feed_run(conn, 30, ok=[a], failed={b: "HTTPError: 404"})
    _feed_run(conn, 20, ok=[a], failed={b: "HTTPError: 404"})
    _feed_run(conn, 1, ok=[a], failed={b: "HTTPError: 410 gone"})
    assert c.get("/health/show").json()["feeds_failing"] == {b: {"since": first, "error": "HTTPError: 410 gone"}}


def test_feeds_failing_since_is_the_start_of_the_current_streak(lib):
    cfg, c, conn = lib
    b = cfg.sources[1].name
    _feed_run(conn, 60, failed={b: "old outage"})
    _feed_run(conn, 50, ok=[b])                      # recovered, then broke again
    since = _feed_run(conn, 40, failed={b: "new"})
    _feed_run(conn, 2, failed={b: "new"})
    assert c.get("/health/show").json()["feeds_failing"][b]["since"] == since


def test_feeds_failing_ignores_a_feed_with_a_success_in_the_window(lib):
    cfg, c, conn = lib
    b = cfg.sources[1].name
    _feed_run(conn, 40, failed={b: "x"})
    _feed_run(conn, 10, ok=[b])
    _feed_run(conn, 1, failed={b: "x"})              # failing again, but for 1 h only
    assert c.get("/health/show").json()["feeds_failing"] == {}


def test_feeds_failing_ignores_a_feed_failing_for_less_than_the_window(lib):
    cfg, c, conn = lib
    b = cfg.sources[1].name
    _feed_run(conn, 30, ok=[b])
    _feed_run(conn, 5, failed={b: "x"})
    _feed_run(conn, 1, failed={b: "x"})
    assert c.get("/health/show").json()["feeds_failing"] == {}


def test_feeds_failing_lists_every_feed_when_the_network_is_down(lib):
    cfg, c, conn = lib
    down = {s.name: "URLError: down" for s in cfg.sources}
    _feed_run(conn, 26, failed=down)
    _feed_run(conn, 1, failed=down)
    assert set(c.get("/health/show").json()["feeds_failing"]) == {s.name for s in cfg.sources}


def test_feeds_failing_ignores_runs_without_per_feed_meta(lib):
    cfg, c, conn = lib
    b = cfg.sources[1].name
    _feed_run(conn, 40, meta=False)
    _feed_run(conn, 30, failed={b: "x"})
    _feed_run(conn, 1, meta=False)                   # newer row without info neither clears nor extends
    assert b in c.get("/health/show").json()["feeds_failing"]
    conn.execute("delete from runs")
    _feed_run(conn, 40, meta=False)
    assert c.get("/health/show").json()["feeds_failing"] == {}


# ---- the MP3 and the podcast feed -------------------------------------------------------------
import xml.etree.ElementTree as ET

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def assembled_day(cfg, date, body=b"ID3fake", **ready):
    d = cfg.paths.var_dir / "show" / date
    d.mkdir(parents=True, exist_ok=True)
    (d / "show.mp3").write_bytes(body)
    (d / "READY").write_text(json.dumps({"segments": {}, "meta": {}, "show": "show.mp3", **ready}))
    return d


def test_show_mp3_serves_an_assembled_day(ondemand):
    cfg, c = ondemand
    assembled_day(cfg, "2026-09-20", b"ID3-old")
    r = c.get("/shows/2026-09-20.mp3")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and r.content == b"ID3-old"
    assert c.get("/shows/2026-09-20.mp3", headers={"Range": "bytes=0-2"}).content == b"ID3"   # players seek


def test_show_mp3_rejects_bad_dates_and_unknown_days(ondemand):
    _, c = ondemand
    assert c.get("/shows/1999-01-01.mp3").status_code == 404
    assert c.get("/shows/not-a-date.mp3").status_code == 422
    assert c.get("/shows/..%2Fx.mp3").status_code in (404, 422)


def test_show_mp3_of_today_assembles_on_demand(lib, monkeypatch):
    cfg, c, conn = lib
    add_episode(conn, cfg, 1, "A", seconds=600)
    real = queue.assemble
    monkeypatch.setattr(queue, "assemble", lambda cn, cf, d: real(
        cn, cf, d, concat=lambda paths, out: out.write_bytes(b"ID3" + b"|".join(p.read_bytes() for p in paths)),
        probe=lambda p: 600.0))
    r = c.get(f"/shows/{DATE}.mp3")
    assert r.status_code == 200 and r.content == b"ID3ID3ep1"
    assert conn.execute("select assigned_date from episodes").fetchone()[0] == DATE


def test_show_mp3_of_another_day_never_assembles(lib, monkeypatch):
    _, c, _ = lib
    monkeypatch.setattr(queue, "assemble", lambda *a: pytest.fail("only today is built on demand"))
    assert c.get("/shows/2026-09-01.mp3").status_code == 404


def test_show_mp3_assembly_failure_is_reported(lib, monkeypatch):
    _, c, _ = lib

    def boom(*a, **k):
        raise queue.QueueError("ffmpeg concat failed")

    monkeypatch.setattr(queue, "assemble", boom)
    r = c.get(f"/shows/{DATE}.mp3")
    assert r.status_code == 500 and "could not assemble" in r.json()["detail"]


def test_show_mp3_nothing_ready_today_is_404(lib):
    _, c, _ = lib
    assert c.get(f"/shows/{DATE}.mp3").status_code == 404      # real assemble: nothing ready writes nothing


def test_day_assembled_before_v3_has_no_mp3(ondemand):
    cfg, c = ondemand
    archived_show(cfg.paths.var_dir / "show", "2026-09-20", ["A"])     # READY without "show"
    assert c.get("/shows/2026-09-20.mp3").status_code == 404


def test_latest_is_todays_show(ondemand):
    cfg, c = ondemand
    assembled_day(cfg, DATE, b"ID3-today")
    assembled_day(cfg, "2026-09-26", b"ID3-yesterday")
    assert c.get("/latest.mp3").content == b"ID3-today"


def test_latest_falls_back_to_the_newest_show(lib, monkeypatch):
    cfg, c, _ = lib
    assembled_day(cfg, "2026-09-20", b"ID3-old")
    assembled_day(cfg, "2026-09-25", b"ID3-newer")
    c.app.state.now = lambda: dt.datetime(2026, 9, 27, 4, 0, tzinfo=dt.UTC)             # before assemble_at: no attempt to build
    monkeypatch.setattr(queue, "assemble", lambda *a: pytest.fail("before assemble_at"))
    assert c.get("/latest.mp3").content == b"ID3-newer"


def test_latest_assembles_today_once_past_assemble_at(lib, monkeypatch):
    cfg, c, _ = lib
    assembled_day(cfg, "2026-09-25", b"ID3-newer")
    c.app.state.now = lambda: dt.datetime(2026, 9, 27, 5, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(queue, "assemble", lambda cn, cf, d: assembled_day(cfg, d, b"ID3-fresh"))
    assert c.get("/latest.mp3").content == b"ID3-fresh"


def test_latest_survives_a_failed_assembly(lib, monkeypatch):
    cfg, c, _ = lib
    assembled_day(cfg, "2026-09-25", b"ID3-newer")
    c.app.state.now = lambda: dt.datetime(2026, 9, 27, 9, 0, tzinfo=dt.UTC)

    def boom(*a):
        raise queue.QueueError("no ffmpeg")

    monkeypatch.setattr(queue, "assemble", boom)
    assert c.get("/latest.mp3").content == b"ID3-newer"


def test_latest_404_when_there_is_no_show(lib):
    _, c, _ = lib
    c.app.state.now = lambda: dt.datetime(2026, 9, 27, 9, 0, tzinfo=dt.UTC)
    assert c.get("/latest.mp3").status_code == 404


def feed_of(c, **kw):
    r = c.get("/feed.xml", **kw)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/rss+xml")
    return ET.fromstring(r.content)


def test_feed_is_empty_but_valid_without_shows(ondemand):
    cfg, c = ondemand
    ch = feed_of(c).find("channel")
    assert ch.findtext("title") == cfg.feed.title and ch.findall("item") == []


def test_feed_lists_newest_shows_with_enclosures(ondemand):
    cfg, c = ondemand
    assembled_day(cfg, "2026-09-25", b"x" * 10, ready_at="2026-09-25T05:01:00-05:00", show_seconds=1830.4,
                  meta={"A": {"title": "Agents & <evals>"}, "B": {"title": "Pricing"}})
    assembled_day(cfg, "2026-09-26", b"y" * 20)       # no ready_at / seconds: falls back to the file's mtime
    items = feed_of(c).find("channel").findall("item")
    assert [i.findtext("guid") for i in items] == ["2026-09-26", "2026-09-25"]
    new, old = items
    assert new.findtext("title") == "Daily Brief — 2026-09-26" and new.find(f"{ITUNES}duration") is None
    enc = new.find("enclosure")
    assert enc.get("url") == "http://testserver/shows/2026-09-26.mp3" and enc.get("length") == "20"
    assert enc.get("type") == "audio/mpeg"
    assert old.find("enclosure").get("length") == "10" and old.findtext(f"{ITUNES}duration") == "1830"
    assert old.findtext("description") == "Agents & <evals> · Pricing"      # escaped on the wire, plain after parsing
    assert old.findtext("pubDate") == "Fri, 25 Sep 2026 05:01:00 -0500" and new.findtext("pubDate")


def test_feed_limit_and_base_url_come_from_config(tmp_path):
    cfg = load_config(write_config(tmp_path, {"items = 14": "items = 2", 'base_url = ""': 'base_url = "https://brief.example/"'}))
    db.connect(cfg.paths.var_dir / "brief.db")
    for d in ("2026-09-24", "2026-09-25", "2026-09-26"):
        assembled_day(cfg, d)
    c = TestClient(api.create_app(cfg, today=lambda: DATE))
    items = feed_of(c).find("channel").findall("item")
    assert [i.findtext("guid") for i in items] == ["2026-09-26", "2026-09-25"]
    assert items[0].find("enclosure").get("url") == "https://brief.example/shows/2026-09-26.mp3"


def test_feed_ignores_days_without_a_file_and_does_not_need_the_archive(ondemand):
    cfg, c = ondemand
    d = assembled_day(cfg, "2026-09-25")
    (d / "show.mp3").unlink()                                   # READY names a file that is gone
    archived_show(cfg.paths.archive_dir, "2026-09-20", ["A"])   # the archive is not consulted
    assert feed_of(c).find("channel").findall("item") == []
