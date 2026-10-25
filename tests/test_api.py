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


class FakePlayer:
    def __init__(self):
        self.sent = []

    def command(self, *args):
        self.sent.append(list(args))
        if args[:2] == ("get_property", "playback-time"):
            return 125.0
        if args[:2] == ("get_property", "playlist-pos"):
            return 1
        return None


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
    (cfg.paths.var_dir / "now_playing.json").write_text(json.dumps({"date": DATE, "entries": [
        {"kind": "alarm", "label": "alarm.mp3", "seconds": 1800, "starts_at_s": 0},
        {"kind": "segment", "label": "B", "seconds": 600, "starts_at_s": 1800}]}))
    (show / "B.mp3").write_bytes(b"x")
    player = FakePlayer()
    launched = []
    app = api.create_app(cfg, player=player, today=lambda: DATE, launcher=lambda p: launched.append(Path(p).read_text()))
    app.state.test_launched = launched
    app.state.probe = lambda p: 600.0
    return TestClient(app), player, conn


def test_health(client):
    c, _, _ = client
    r = c.get("/health").json()
    assert r["ok"] and r["date"] == DATE and r["ready_segments"] == ["B"]


def test_health_lists_enabled_segments_that_are_not_ready(client):
    c, _, _ = client
    r = c.get("/health").json()
    assert set(r["enabled_segments"]) >= {"B"}
    assert r["missing_segments"] == sorted(set(r["enabled_segments"]) - {"B"})


def test_today_has_kept_and_cut(client):
    c, _, _ = client
    r = c.get("/today").json()
    seg = r["segments"]["B"]
    assert seg["title"] == RUNDOWN["segment_title"]
    assert [k["cluster_id"] for k in seg["kept"]] == ["k1", "k2", "k3", "k4", "k5"]
    assert len(seg["cut"]) == len(RUNDOWN["cut"]) and seg["so_what"]


def test_now_playing_uses_player_position(client):
    c, _, _ = client
    r = c.get("/now-playing").json()
    assert r["playing"] and r["entry"]["label"] == "B" and r["position_s"] == 125.0


def test_feedback_recorded_and_validated(client):
    c, _, conn = client
    ok = c.post("/feedback", json={"date": DATE, "segment": "B", "cluster_id": "k2", "kind": "deeper", "note": "more"})
    assert ok.status_code == 200
    assert conn.execute("select cluster_id, kind, note from feedback").fetchall() == [("k2", "deeper", "more")]
    assert c.post("/feedback", json={"date": DATE, "segment": "B", "cluster_id": "k99", "kind": "up"}).status_code == 404
    assert c.post("/feedback", json={"date": DATE, "segment": "B", "cluster_id": "k1", "kind": "love"}).status_code == 422


@pytest.mark.parametrize("action,expected", [
    ("pause", ["set_property", "pause", True]), ("resume", ["set_property", "pause", False]),
    ("skip", ["playlist-next"]), ("back", ["seek", -15, "relative"]),
])
def test_player_controls(client, action, expected):
    c, player, _ = client
    assert c.post(f"/player/{action}").status_code == 200
    assert player.sent[-1] == expected


def test_volume_bounds(client):
    c, player, _ = client
    assert c.post("/player/volume", json={"value": 55}).status_code == 200
    assert player.sent[-1] == ["set_property", "volume", 55]
    assert c.post("/player/volume", json={"value": 150}).status_code == 422


def test_player_not_running(client, tmp_path):
    c, player, _ = client

    def down(*a):
        raise api.PlayerUnavailable("no socket")

    player.command = down
    assert c.post("/player/pause").status_code == 409
    assert c.get("/now-playing").json()["playing"] is False


def test_export_feedback_and_ledger(client):
    c, _, conn = client
    c.post("/feedback", json={"date": DATE, "segment": "B", "cluster_id": "k1", "kind": "up"})
    conn.execute("insert into ledger(topic_key, first_covered, last_covered) values ('evals','2026-09-27','2026-09-27')")
    conn.execute("update rundown_items set topic='eval the trace', reason='why k1' where cluster_id='k1'")
    r = c.get("/export", params={"since": "2026-09-01"}).json()
    assert r["feedback"][0]["kind"] == "up" and r["ledger"][0]["topic_key"] == "evals"
    assert r["feedback"][0]["topic"] == "eval the trace" and r["feedback"][0]["why"] == "why k1"


def test_phone_page_served(client):
    c, _, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "/today" in r.text


# ---- /today before assembly, /health/show, /health/llm ---------------------------------------
from tests.test_queue import add_episode


@pytest.fixture
def lib(tmp_path):
    """(cfg, client, db connection) with a player that is not running and an empty library."""
    cfg = load_config(write_config(tmp_path))  # every runtime path redirected into tmp_path
    conn = db.connect(cfg.paths.var_dir / "brief.db")
    launched = []
    app = api.create_app(cfg, player=DownPlayer(), today=lambda: DATE,
                         launcher=lambda playlist: launched.append(Path(playlist).read_text()))
    app.state.probe = lambda p: 600.0
    app.state.test_launched = launched
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
    c, _, _ = client      # the client fixture has an assembled DATE
    r = c.get("/today").json()
    assert r["up_next"] == [] and "B" in r["segments"]
    assert c.get("/today", params={"date": "2026-01-01"}).json()["up_next"] == []


def test_play_without_a_date_assembles_today_first(lib, monkeypatch):
    from daily_brief import queue
    cfg, c, conn = lib
    add_episode(conn, cfg, 1, "A", seconds=600)
    real = queue.assemble
    monkeypatch.setattr(queue, "assemble", lambda cn, cf, d: real(
        cn, cf, d, concat=lambda paths, out: out.write_bytes(b"x"), probe=lambda p: 600.0))
    r = c.post("/player/play").json()
    assert r["action"] == "started" and r["date"] == DATE and r["segments"] == ["A"]
    assert conn.execute("select assigned_date from episodes").fetchone()[0] == DATE
    assert c.get("/today").json()["up_next"] == []


def test_play_assembly_failure_is_reported_not_a_bare_500(lib, monkeypatch):
    from daily_brief import queue
    _, c, _ = lib

    def boom(*a, **k):
        raise queue.QueueError("ffmpeg concat failed")

    monkeypatch.setattr(queue, "assemble", boom)
    r = c.post("/player/play")
    assert r.status_code == 500 and "could not assemble" in r.json()["detail"]


def test_health_alarm_is_gone(client):
    c, _, _ = client
    assert c.get("/health/alarm").status_code == 404


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
    c, _, _ = client
    html = c.get("/").text
    for needle in ("Up next", "up_next", "/health/show", "voice spend today", 'id="spend"'):
        assert needle in html


# ---- Play / Stop for the TV card --------------------------------------------------------------
class DownPlayer:
    def __init__(self):
        self.sent = []

    def command(self, *args):
        self.sent.append(list(args))
        raise api.PlayerUnavailable("no socket")


@pytest.fixture
def ondemand(tmp_path):
    cfg = load_config(write_config(tmp_path))  # every runtime path redirected into tmp_path
    db.connect(cfg.paths.var_dir / "brief.db")
    launched = []
    app = api.create_app(cfg, player=DownPlayer(), today=lambda: DATE,
                         launcher=lambda playlist: launched.append(Path(playlist).read_text()))
    app.state.probe = lambda p: 600.0
    return cfg, TestClient(app), launched


def ready_show(cfg, date, segs):
    d = cfg.paths.var_dir / "show" / date
    d.mkdir(parents=True, exist_ok=True)
    for s in segs:
        (d / f"{s}.mp3").write_bytes(b"x")
    (d / "READY").write_text(json.dumps({"segments": {s: f"{s}.mp3" for s in segs}, "meta": {}}))


def test_play_resumes_when_show_running(client):
    c, player, _ = client
    assert c.post("/player/play").json()["action"] == "resumed"
    assert player.sent[-1] == ["set_property", "pause", False]


def test_play_starts_todays_segments_on_demand(ondemand):
    cfg, c, launched = ondemand
    ready_show(cfg, DATE, ["A", "B"])
    r = c.post("/player/play").json()
    assert r["action"] == "started" and r["date"] == DATE
    assert launched and launched[0].index("/A.mp3") < launched[0].index("/B.mp3")
    np_ = json.loads((cfg.paths.var_dir / "now_playing.json").read_text())
    assert [e["label"] for e in np_["entries"]] == ["A", "B"]


def test_play_falls_back_to_latest_ready_show(ondemand):
    cfg, c, launched = ondemand
    ready_show(cfg, "2026-09-25", ["B"])
    r = c.post("/player/play").json()
    assert r["action"] == "started" and r["date"] == "2026-09-25" and "/B.mp3" in launched[0]


def test_play_with_nothing_ready_is_409(ondemand):
    _, c, launched = ondemand
    assert c.post("/player/play").status_code == 409 and not launched


def test_stop_pauses(client):
    c, player, _ = client
    assert c.post("/player/stop").status_code == 200
    assert player.sent[-1] == ["set_property", "pause", True]


# ---- library, replay, seek (TV strip v2) ------------------------------------------------------
def archived_show(root, date, segs, title="T"):
    d = root / date
    d.mkdir(parents=True, exist_ok=True)
    for s in segs:
        (d / f"{s}.mp3").write_bytes(b"x")
    (d / "READY").write_text(json.dumps({"segments": {s: f"{s}.mp3" for s in segs},
                                         "meta": {s: {"title": f"{title} {s}", "seconds": 600} for s in segs}}))


def test_library_merges_local_and_archive_newest_first(ondemand):
    cfg, c, _ = ondemand
    archived_show(cfg.paths.var_dir / "show", DATE, ["A", "B"], "Today")
    archived_show(cfg.paths.archive_dir, "2026-09-20", ["B"], "Old")
    archived_show(cfg.paths.archive_dir, DATE, ["A", "B"], "Dup")  # same day in both: local wins
    days = c.get("/library").json()["days"]
    assert [d["date"] for d in days] == [DATE, "2026-09-20"]
    assert [s["segment"] for s in days[0]["segments"]] == ["A", "B"]
    assert days[0]["segments"][0]["title"] == "Today A" and days[1]["segments"][0]["title"] == "Old B"


def test_play_specific_recording_from_archive(ondemand):
    cfg, c, launched = ondemand
    archived_show(cfg.paths.archive_dir, "2026-09-20", ["A", "B"])
    r = c.post("/player/play", json={"date": "2026-09-20", "segment": "B"}).json()
    assert r["action"] == "started" and r["date"] == "2026-09-20" and r["segments"] == ["B"]
    assert str(cfg.paths.archive_dir / "2026-09-20" / "B.mp3") in launched[0]


def test_specific_pick_launches_even_while_something_plays(client):
    c, player, _ = client  # this fixture's player answers: a show is running
    launched = c.app.state.test_launched
    r = c.post("/player/play", json={"date": DATE, "segment": "B"}).json()  # client fixture has READY B
    assert r["action"] == "started" and launched, "a specific pick must start that recording, not resume"
    assert ["set_property", "pause", False] not in player.sent


def test_unknown_recording_is_404(client):
    c, _, _ = client
    assert c.post("/player/play", json={"date": "1999-01-01", "segment": "B"}).status_code == 404


def test_seek_absolute_and_relative(client):
    c, player, _ = client
    assert c.post("/player/seek", json={"to_s": 125}).status_code == 200
    assert player.sent[-1] == ["seek", 125, "absolute"]
    assert c.post("/player/forward").status_code == 200
    assert player.sent[-1] == ["seek", 30, "relative"]
    assert c.post("/player/rewind").status_code == 200
    assert player.sent[-1] == ["seek", -30, "relative"]


def test_seek_rejects_nonsense(client):
    c, _, _ = client
    assert c.post("/player/seek", json={"to_s": -5}).status_code == 422
    assert c.post("/player/seek", json={}).status_code == 422


def test_now_playing_includes_duration_and_paused(client):
    c, player, _ = client
    orig = player.command

    def cmd(*args):
        if args[:2] == ("get_property", "duration"):
            return 612.5
        if args[:2] == ("get_property", "pause"):
            return True
        return orig(*args)

    player.command = cmd
    r = c.get("/now-playing").json()
    assert r["duration_s"] == 612.5 and r["paused"] is True


def test_alarm_defaults_off(client):
    c, _, _ = client
    r = c.get("/alarm").json()
    assert r["enabled"] is False and r["sound"] == "podcast" and r["next_fire"] is None
    assert r["last_run"] is None


def test_alarm_put_roundtrip_and_next_fire(client):
    c, _, _ = client
    body = {"enabled": True, "time": "07:30", "days": [0, 1, 2, 3, 4], "sound": "spotify"}
    assert c.put("/alarm", json=body).status_code == 200
    got = c.get("/alarm").json()
    assert {k: got[k] for k in body} == body
    assert got["next_fire"] is not None and got["next_fire"][11:16] == "07:30"


@pytest.mark.parametrize("bad", [
    {"enabled": True, "time": "25:00", "days": [0], "sound": "podcast"},
    {"enabled": True, "time": "06:00", "days": [], "sound": "podcast"},
    {"enabled": True, "time": "06:00", "days": [0], "sound": "radio"},
    {"enabled": True, "time": "06:00", "days": [9], "sound": "podcast"},
    {"enabled": True, "time": "06:00", "days": [0]},
])
def test_alarm_put_rejects_and_keeps_file(client, bad):
    c, _, _ = client
    good = {"enabled": True, "time": "06:30", "days": [0], "sound": "podcast"}
    assert c.put("/alarm", json=good).status_code == 200
    assert c.put("/alarm", json=bad).status_code == 422
    assert c.get("/alarm").json()["time"] == "06:30"


def test_alarm_shows_the_last_alarm_run(client):
    c, _, conn = client
    run = db.start_run(conn, DATE, "alarm")
    db.finish_run(conn, run, "ok", meta={"sound": "podcast", "used": "spotify", "fallback_reason": "x"})
    last = c.get("/alarm").json()["last_run"]
    assert last["status"] == "ok" and last["meta"]["used"] == "spotify"


def test_phone_page_has_alarm_controls(client):
    c, _, _ = client
    html = c.get("/").text
    for needle in ('id="alarm-enabled"', 'id="alarm-time"', 'id="alarm-sound"', "/alarm"):
        assert needle in html


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
