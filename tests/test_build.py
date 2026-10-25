import datetime as dt
import json
import shutil
import subprocess
from types import SimpleNamespace as S

import numpy as np
import pytest

from daily_brief import brief, build, collect, db, llm, queue, transcribe, voice
from daily_brief.config import load_config
from tests.conftest import REPO, write_config

NOW = dt.datetime(2026, 10, 26, 10, 0, tzinfo=dt.UTC)
TODAY = NOW.date().isoformat()
EMPTY_RSS = b"<rss><channel></channel></rss>"
EMPTY_ATOM = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
YT = "https://www.youtube.com/watch?v={}"


def empty_feed(url):
    return EMPTY_ATOM if "youtube.com" in url else EMPTY_RSS


def valid_episode(**over):
    d = {"title": "Evals in practice", "why": "You are building agents, so this shows you what to measure first.",
         "chapters": [{"topic": "eval the trace", "why": "It is the check your pipeline lacks.", "priority": "p2"}],
         "so_what": "Add one trace check to your pipeline this week.",
         "turns": [{"speaker": "AB"[i % 2], "text": "Here is one plain sentence about the idea."} for i in range(6)]}
    return d | over


def fake_model(output=None):
    """An LLM runner that answers with `output` (a dict, or a callable prompt -> dict) in a json fence."""
    calls = []

    def run(prompt):
        calls.append(prompt)
        out = output(prompt) if callable(output) else (valid_episode() if output is None else output)
        return 0, "```json\n" + json.dumps(out) + "\n```", ""

    run.calls = calls
    run.auth_patterns = []
    return run


def fake_heygen(seconds=30, cost=0.001, fail_on_call=None, fail_with=None):
    """A HeyGen transport + billed-cost read-back, both offline. 30 s of pcm per turn."""
    calls = []
    pcm = (np.sin(2 * np.pi * 330 * np.arange(seconds * 44100) / 44100) * 3000).astype("<i2").tobytes()

    def post(body):
        calls.append(body)
        if fail_on_call is not None and len(calls) == fail_on_call:
            raise fail_with or voice.VoiceError("HeyGen TTS HTTP 400: bad voice")
        return pcm, f"gen-{len(calls)}"

    post.calls = calls
    return post, lambda gen_id: cost


def fake_master(wav, out, min_s, *args):
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"mp3")
    return {"seconds": 90.0, "file": out.name}


class Captions:
    def __init__(self, text="[00:00] " + "the talk goes on " * 40, deferred=False):
        self.text, self.deferred, self.calls = text, deferred, []

    def __call__(self, url):
        self.calls.append(url)
        if self.deferred:
            raise transcribe.TranscriptDeferred("caption source rate-limited")
        return self.text


def make_deps(model=None, heygen=None, captions=None, master=fake_master, now=NOW):
    def download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"mp3")

    post, fetch = heygen or fake_heygen()
    return build.Deps(runner=model or fake_model(), download=download,
                      make_whisper=lambda: (lambda p: [S(start=0, text="words " * 400)]), now=lambda: now,
                      prompts_dir=REPO / "prompts", fetch=empty_feed, captions=captions or Captions(),
                      heygen_post=post, heygen_fetch=fetch, sleep=lambda s: None, master=master,
                      assemble=lambda conn, cfg, date: {})


@pytest.fixture
def env(tmp_path):
    # min_minutes 0.1: the fake episodes are a few dozen words, not four minutes of speech
    cfg = load_config(write_config(tmp_path, {"min_minutes = 4 ": "min_minutes = 0.1 "}))
    b = brief.load_brief(cfg.brief.path, cfg.brief.tv_forbidden_terms)
    conn = db.connect(cfg.paths.var_dir / "brief.db")
    ids = collect.sync_sources(conn, [collect.SourceSpec(**s.__dict__) for s in cfg.sources])
    env = S(cfg=cfg, b=b, conn=conn, src={s.name: ids[s.url] for s in cfg.sources}, n=0)
    return env


LENNY, GROWTH, AIE, WELCH = "Lenny's Podcast", "The Growth Podcast (Aakash Gupta)", "AI Engineer", "Welch Labs"


def add(env, source, title, days_ago=1, backcat=False, status="discovered", transcript=None):
    """An item of a configured source. YouTube sources get a watch URL (so captions apply), podcasts an mp3."""
    env.n += 1
    published = (NOW - dt.timedelta(days=days_ago, minutes=env.n)).isoformat()  # later adds are slightly older
    yt = source in (AIE, WELCH)
    item_id = env.conn.execute(
        "insert into items(source_id, guid, title, url, audio_url, published_at, is_backcatalog, status) "
        "values (?,?,?,?,?,?,?,?)",
        (env.src[source], f"g{env.n}", title, "https://x", YT.format(f"v{env.n}") if yt else f"https://x/{env.n}.mp3",
         published, int(backcat), status)).lastrowid
    if transcript:
        path = env.cfg.paths.var_dir / "transcripts" / f"{item_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(transcript)
        env.conn.execute("insert into transcripts(item_id, method, path, words) values (?, 'rss', ?, 5)",
                         (item_id, str(path)))
    return item_id


def episode_row(env, item_id):
    return env.conn.execute("select status, title, why, path, seconds, chars, billed_usd, assigned_date "
                            "from episodes where item_id = ?", (item_id,)).fetchone()


def item_row(env, item_id):
    return env.conn.execute("select status, attempts, fail_reason from items where id = ?", (item_id,)).fetchone()


def ready_episode(env, item_id, segment="B", assigned=None):
    env.conn.execute("insert into episodes(item_id, segment, status, built_at, assigned_date) "
                     "values (?, ?, 'ready', ?, ?)", (item_id, segment, NOW.isoformat(), assigned))


# ---- build_one --------------------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_build_one_happy_path_writes_library_and_episode_row(env):
    item = add(env, LENNY, "Evals for AI PMs")
    env.conn.execute("insert into ledger(topic_key, first_covered, last_covered, times) "
                     "values ('agent-memory', '2026-10-20', '2026-10-20', 1)")
    model = fake_model()
    heygen = fake_heygen()
    res = build.build_one(env.conn, env.cfg, env.b, item, make_deps(model, heygen, master=build.mix.master_segment))
    assert res["status"] == "ready" and res["segment"] == "B" and res["billed_usd"] == pytest.approx(0.006)
    lib = env.cfg.paths.var_dir / "library" / str(item)
    assert (lib / "episode.mp3").stat().st_size > 0
    meta = json.loads((lib / "episode.json").read_text())
    assert meta["title"] == "Evals in practice" and meta["creator"] == LENNY and len(meta["turns"]) == 6
    assert not list(lib.glob("*.tmp")) and not list(env.cfg.paths.var_dir.rglob("*.wav"))
    status, title, why, path, seconds, chars, billed, assigned = episode_row(env, item)
    assert (status, title, assigned) == ("ready", "Evals in practice", None)
    assert why.startswith("You are") and path == str(lib / "episode.mp3") and seconds > 60 and chars > 0
    assert json.loads(env.conn.execute("select chapters from episodes").fetchone()[0])[0]["priority"] == "p2"
    assert env.conn.execute("select so_what from episodes").fetchone()[0].startswith("Add one")
    assert len(heygen[0].calls) == 6 and heygen[0].calls[0]["voice"] == "weston" and heygen[0].calls[1]["voice"] == "meredith"
    assert billed == pytest.approx(0.006)
    assert env.conn.execute("select count(*) from voice_costs where date = ? and engine = 'heygen'",
                            (TODAY,)).fetchone()[0] == 1
    assert len(model.calls) == 1  # ONE Claude call for the whole episode
    prompt = model.calls[0]
    for section in ("=== BRIEF ===", "=== HISTORY ===", "=== EPISODE ===", "=== BUDGET_MINUTES ===",
                    "=== TRANSCRIPT ==="):
        assert section in prompt
    assert "agent-memory" in prompt and "Evals for AI PMs" in prompt  # history and episode metadata
    assert "words words" in prompt  # the podcast was transcribed by the (fake) whisper
    assert '"is_backcatalog": false' in prompt and LENNY in prompt


def test_build_one_prompt_carries_limits_from_config(env):
    item = add(env, AIE, "t")  # YouTube: the captions text is the transcript
    model = fake_model()
    long_talk = "[00:00] " + "the talk goes on " * 1500  # 6,000 words: 0.4 share = 16 min
    build.build_one(env.conn, env.cfg, env.b, item, make_deps(model, captions=Captions(long_talk)))
    text = model.calls[0].split("=== BUDGET_MINUTES ===")[1].split("=== TRANSCRIPT ===")[0]
    assert "at most 16 minutes" in text and "at most 90 words" in text and "insane" in text


def words(n):
    return "[00:00] " + "word " * n  # the marker is not a spoken word


def test_episode_max_scales_with_the_source(env):
    # the 2026-10-25 live test: an 18-minute talk (2,781 words) became a 13.7-minute script
    assert build.episode_max_minutes(env.cfg, words(2781)) == 7.4
    assert "at most 7.4 minutes" in build.budget_text(env.cfg, 7.4)


def test_episode_max_is_capped_and_floored(env):
    cfg = load_config(write_config(env.cfg.paths.var_dir.parent))  # the shipped min_minutes = 4
    assert build.episode_max_minutes(cfg, words(20000)) == cfg.build.max_minutes
    assert build.episode_max_minutes(cfg, words(30)) == cfg.build.min_minutes


def test_build_one_validates_with_the_per_episode_max(env):
    item = add(env, AIE, "t")
    seen = []
    real = build.script.validate
    build.script.validate = lambda d, rules: seen.append(rules.max_minutes) or real(d, rules)
    try:
        build.build_one(env.conn, env.cfg, env.b, item, make_deps(captions=Captions(words(2781))))
    finally:
        build.script.validate = real
    assert seen == [7.4]  # the prompt and the validator agree


def test_build_one_skip_records_skipped_no_voice(env):
    item = add(env, LENNY, "A funding round story")
    heygen = fake_heygen()
    res = build.build_one(env.conn, env.cfg, env.b, item,
                          make_deps(fake_model({"skip": True, "reason": "Funding gossip."}), heygen))
    assert res["status"] == "skipped" and heygen[0].calls == []
    assert episode_row(env, item)[:3] == ("skipped", None, "Funding gossip.")
    assert env.conn.execute("select count(*) from voice_costs").fetchone()[0] == 0
    assert not (env.cfg.paths.var_dir / "library" / str(item)).exists()
    assert item not in build.pick(env.conn, env.cfg, env.b, NOW.date())  # never picked again


def test_build_one_deferred_charges_nothing(env):
    item = add(env, AIE, "A talk")
    model, heygen = fake_model(), fake_heygen()
    res = build.build_one(env.conn, env.cfg, env.b, item, make_deps(model, heygen, Captions(deferred=True)))
    assert res["status"] == "deferred" and "rate-limited" in res["reason"]
    assert item_row(env, item)[:2] == ("discovered", 0)  # no attempt charged
    assert model.calls == [] and heygen[0].calls == [] and episode_row(env, item) is None
    assert item in build.pick(env.conn, env.cfg, env.b, NOW.date())  # still first in line next hour


def test_build_one_capped_makes_no_llm_or_voice_call(env):
    item = add(env, LENNY, "t")
    db.add_voice_cost(env.conn, TODAY, "B", "heygen", 30000, 20, 0.14, 0)  # 0.14 + 0.072 estimate > 0.15
    model, heygen, captions = fake_model(), fake_heygen(), Captions()
    res = build.build_one(env.conn, env.cfg, env.b, item, make_deps(model, heygen, captions))
    assert res["status"] == "capped"
    assert model.calls == [] and heygen[0].calls == [] and captions.calls == []
    assert item_row(env, item)[:2] == ("discovered", 0) and episode_row(env, item) is None


def test_the_cap_counts_only_todays_billed_spend(env):
    item = add(env, LENNY, "t")
    db.add_voice_cost(env.conn, "2026-10-25", "B", "heygen", 30000, 20, 5.0, 0)  # yesterday's spend
    res = build.build_one(env.conn, env.cfg, env.b, item, make_deps(master=fake_master))
    assert res["status"] == "ready"


def test_render_heygen_refuses_before_any_request_when_the_script_would_break_the_cap(env, tmp_path):
    db.add_voice_cost(env.conn, TODAY, "B", "heygen", 1, 1, 0.149, 0)
    heygen = fake_heygen()
    with pytest.raises(voice.DailyCapReached, match="daily cap"):
        build.render_heygen(env.conn, env.cfg, TODAY, "B", [{"speaker": "A", "text": "x" * 5000}],
                            tmp_path / "e.wav", heygen[0], heygen[1], lambda s: None)
    assert heygen[0].calls == []


def test_build_one_failure_twice_sets_item_failed(env):
    item = add(env, LENNY, "t")
    bad = fake_model(lambda p: {"title": "no turns"})  # never valid -> LLMError after llm.max_attempts
    heygen = fake_heygen()
    first = build.build_one(env.conn, env.cfg, env.b, item, make_deps(bad, heygen))
    assert first["status"] == "failed" and first["stage"] == "script" and first["item_failed"] is False
    assert item_row(env, item)[:2] == ("transcribed", 1)
    second = build.build_one(env.conn, env.cfg, env.b, item, make_deps(bad, heygen))
    assert second["item_failed"] is True and item_row(env, item)[:2] == ("failed", 2)
    assert "script:" in item_row(env, item)[2] and heygen[0].calls == []
    assert item not in build.pick(env.conn, env.cfg, env.b, NOW.date())


def test_build_one_voice_failure_is_charged_keeps_the_bill_and_leaves_no_wav(env):
    item = add(env, LENNY, "t")
    heygen = fake_heygen(fail_on_call=3)  # a 400: the episode's own problem
    res = build.build_one(env.conn, env.cfg, env.b, item, make_deps(fake_model(), heygen))
    assert res["status"] == "failed" and res["stage"] == "voice" and "400" in res["error"]
    assert item_row(env, item)[1] == 1 and episode_row(env, item) is None
    assert env.conn.execute("select requests, billed_usd from voice_costs").fetchone() == (3, pytest.approx(0.002))
    # only the paid script is kept (no wav, no episode.mp3/json)
    assert not list(env.cfg.paths.var_dir.rglob("*.wav"))
    assert [f.name for f in (env.cfg.paths.var_dir / "library").rglob("*") if f.is_file()] == ["script.json"]


def script_cache(env, item):
    return env.cfg.paths.var_dir / "library" / str(item) / "script.json"


@pytest.mark.parametrize("err", [voice.VoiceUnavailable("HeyGen TTS HTTP 503: down"),
                                 voice.VoiceUnavailable("HeyGen TTS HTTP 402: insufficient credits")])
def test_build_one_provider_outage_is_not_charged_keeps_partial_bill(env, err):
    item = add(env, LENNY, "t")
    res = build.build_one(env.conn, env.cfg, env.b, item, make_deps(heygen=fake_heygen(fail_on_call=3, fail_with=err)))
    assert (res["status"], res["stage"], res["item_failed"], res["provider_down"]) == ("failed", "voice", False, True)
    assert str(err) in res["error"] and item_row(env, item)[1] == 0 and episode_row(env, item) is None
    assert env.conn.execute("select requests, billed_usd from voice_costs").fetchone() == (3, pytest.approx(0.002))


def test_run_stops_after_the_first_item_when_the_provider_is_down(env):
    first, second = add(env, LENNY, "a"), add(env, GROWTH, "b")
    model, heygen = fake_model(), fake_heygen(fail_on_call=1, fail_with=voice.VoiceUnavailable("HeyGen TTS HTTP 503: x"))
    meta = build.run_once(env.conn, env.cfg, make_deps(model, heygen))
    assert len(meta["picked"]) == 1 and len(model.calls) == 1 and len(heygen[0].calls) == 1
    assert meta["outcome"] == "failed" and "HTTP 503" in build.run_failure(meta)
    assert item_row(env, first)[1] == 0 and item_row(env, second)[1] == 0


def test_voice_failure_keeps_the_paid_script_and_the_next_build_reuses_it(env):
    item = add(env, LENNY, "t")
    down = voice.VoiceUnavailable("HeyGen TTS HTTP 503: x")
    first = build.build_one(env.conn, env.cfg, env.b, item, make_deps(heygen=fake_heygen(fail_on_call=1, fail_with=down)))
    cached = json.loads(script_cache(env, item).read_text())
    assert first["llm_ok"] is True and cached["script"] == valid_episode() and cached["llm"]["attempts"] == 1
    assert cached["max_minutes"] > 0
    model, heygen = fake_model(), fake_heygen()
    res = build.build_one(env.conn, env.cfg, env.b, item, make_deps(model, heygen))
    assert model.calls == [] and res["status"] == "ready" and res["script_cached"] is True
    assert "llm_ok" not in res  # a cached run proves nothing about the token
    assert len(heygen[0].calls) == 6 and not script_cache(env, item).exists()  # voiced the cached turns; cache dropped
    assert json.loads((script_cache(env, item).parent / "episode.json").read_text())["llm"]["attempts"] == 1


@pytest.mark.parametrize("content", ["not json {", json.dumps({"script": {"title": "x"}, "llm": {}}),
                                     json.dumps({"script": {"skip": True, "reason": "r"}, "llm": {}}), "[]"])
def test_unusable_script_cache_is_ignored_and_claude_is_called(env, content):
    item = add(env, LENNY, "t")
    script_cache(env, item).parent.mkdir(parents=True)
    script_cache(env, item).write_text(content)
    model = fake_model()
    res = build.build_one(env.conn, env.cfg, env.b, item, make_deps(model))
    assert len(model.calls) == 1 and res["status"] == "ready" and res["llm_ok"] is True and "script_cached" not in res


def test_skip_results_are_not_cached(env):
    item = add(env, LENNY, "t")
    res = build.build_one(env.conn, env.cfg, env.b, item, make_deps(fake_model({"skip": True, "reason": "off"})))
    assert res["status"] == "skipped" and not script_cache(env, item).exists()


def test_prune_library_leaves_the_cache_of_an_item_still_waiting(env):
    from daily_brief import upkeep
    item = add(env, LENNY, "t")
    build.build_one(env.conn, env.cfg, env.b, item, make_deps(heygen=fake_heygen(
        fail_on_call=1, fail_with=voice.VoiceUnavailable("HeyGen TTS HTTP 503: x"))))
    upkeep.prune_library(env.cfg, env.conn, NOW.date() + dt.timedelta(days=999), 1)
    assert script_cache(env, item).exists()


def test_build_one_missing_openrouter_key_charges_nothing_and_skips_claude(env, tmp_path):
    cfg = load_config(write_config(tmp_path, {"min_minutes = 4 ": "min_minutes = 0.1 ",
                                              str(tmp_path / "secrets.env"): str(tmp_path / "none.env")}))
    item = add(env, LENNY, "t")
    model = fake_model()
    deps = make_deps(model)
    deps.heygen_post = deps.heygen_fetch = None  # the real transport, which needs the key
    res = build.build_one(env.conn, cfg, env.b, item, deps)
    assert res["status"] == "failed" and res["stage"] == "voice" and res["item_failed"] is False
    assert item_row(env, item)[1] == 0 and model.calls == []


def test_build_one_transcript_failure_is_not_charged_twice(env):
    item = add(env, AIE, "A talk")
    model = fake_model()
    res = build.build_one(env.conn, env.cfg, env.b, item, make_deps(model, captions=Captions(text="")))
    # YouTube answered "no transcript": transcribe charged ONE attempt; the builder adds none
    assert res["status"] == "failed" and res["stage"] == "transcript"
    assert item_row(env, item)[1] == 1 and model.calls == []


def test_build_one_uses_a_transcript_already_on_disk(env):
    item = add(env, LENNY, "t", transcript="[00:01] already here and kept")
    model, captions = fake_model(), Captions()
    build.build_one(env.conn, env.cfg, env.b, item, make_deps(model, captions=captions))
    assert "already here and kept" in model.calls[0] and captions.calls == []


def test_auth_error_propagates_from_build_one_and_charges_nothing(env):
    item = add(env, LENNY, "t")

    def logged_out(prompt):
        return 1, "Failed to authenticate", ""

    logged_out.auth_patterns = ["Failed to authenticate"]
    heygen = fake_heygen()
    with pytest.raises(llm.LLMAuthError):
        build.build_one(env.conn, env.cfg, env.b, item, make_deps(logged_out, heygen))
    assert item_row(env, item)[1] == 0 and heygen[0].calls == [] and episode_row(env, item) is None


# ---- pick -------------------------------------------------------------------------------------------

def test_pick_prefers_segment_with_empty_queue(env):
    a = add(env, AIE, "AI evals for agents")
    b1 = add(env, LENNY, "AI evals for PMs")
    ready_episode(env, add(env, WELCH, "old one", days_ago=30), segment="A")  # A already has one waiting
    assert build.pick(env.conn, env.cfg, env.b, NOW.date())[:2] == [b1, a]
    ready_episode(env, add(env, GROWTH, "old two", days_ago=30), segment="B")
    ready_episode(env, add(env, GROWTH, "old three", days_ago=31), segment="B")  # now B has more waiting than A
    assert build.pick(env.conn, env.cfg, env.b, NOW.date())[:2] == [a, b1]


def test_pick_ranks_new_items_by_keyword_match_then_recency(env):
    plain = add(env, LENNY, "Something unrelated", days_ago=1)
    match = add(env, GROWTH, "Agent reliability and evals", days_ago=3)
    assert build.pick(env.conn, env.cfg, env.b, NOW.date())[:2] == [match, plain]


def test_pick_pushes_ignore_list_terms_down(env):
    ignored = add(env, WELCH, "Crypto funding rounds", days_ago=1)  # newer, but on the ignore list
    plain = add(env, AIE, "Something unrelated", days_ago=2)
    assert build.pick(env.conn, env.cfg, env.b, NOW.date()) == [plain, ignored]


def test_pick_backcatalog_only_when_queue_empty(env):
    back = add(env, LENNY, "AI evals deep dive", days_ago=60, backcat=True)
    assert build.pick(env.conn, env.cfg, env.b, NOW.date()) == [back]
    ready_episode(env, add(env, GROWTH, "waiting", days_ago=30), segment="B")
    assert build.pick(env.conn, env.cfg, env.b, NOW.date()) == []
    env.conn.execute("update episodes set assigned_date = ?", (TODAY,))  # taken by a morning: the queue is empty again
    assert build.pick(env.conn, env.cfg, env.b, NOW.date()) == [back]


def test_pick_gives_one_backcatalog_item_per_empty_segment(env):
    add(env, LENNY, "AI evals deep dive", days_ago=60, backcat=True)
    add(env, GROWTH, "Another deep dive", days_ago=70, backcat=True)
    add(env, WELCH, "Neural nets", days_ago=80, backcat=True)
    picks = build.pick(env.conn, env.cfg, env.b, NOW.date())
    segs = [env.conn.execute("select s.segment from items i join sources s on s.id = i.source_id where i.id = ?",
                             (i,)).fetchone()[0] for i in picks]
    assert sorted(segs) == ["A", "B"]


def test_pick_respects_per_source_daily_limit(env):
    first = add(env, LENNY, "AI evals one", days_ago=1)
    second = add(env, LENNY, "AI evals two", days_ago=2)
    other = add(env, GROWTH, "Growth piece", days_ago=2)
    # one Lenny's item per day: only the best-ranked one is a candidate, the other creator is unaffected
    picks = build.pick(env.conn, env.cfg, env.b, NOW.date())
    assert first in picks and other in picks and second not in picks
    ready_episode(env, first)
    env.conn.execute("update episodes set built_at = ?", (NOW.isoformat(),))  # built today
    picks = build.pick(env.conn, env.cfg, env.b, NOW.date())
    assert second not in picks and other in picks
    env.conn.execute("update episodes set built_at = ?", ((NOW - dt.timedelta(days=1)).isoformat(),))
    assert second in build.pick(env.conn, env.cfg, env.b, NOW.date())  # yesterday's build does not count today


def test_pick_ignores_items_outside_new_window(env):
    add(env, LENNY, "AI evals", days_ago=8)  # window is 7 days and it is not flagged back catalog
    inside = add(env, GROWTH, "AI evals", days_ago=6)
    assert build.pick(env.conn, env.cfg, env.b, NOW.date()) == [inside]


def test_pick_skips_failed_and_already_built_items(env):
    add(env, LENNY, "failed one", status="failed")
    built = add(env, GROWTH, "built one")
    ready_episode(env, built)
    assert build.pick(env.conn, env.cfg, env.b, NOW.date()) == []


# ---- the run ----------------------------------------------------------------------------------------

def test_run_deferred_a_does_not_block_b(env):
    a = add(env, AIE, "AI evals talk")
    b1 = add(env, LENNY, "Agents")
    model = fake_model()
    meta = build.run_once(env.conn, env.cfg, make_deps(model, captions=Captions(deferred=True), master=fake_master))
    # A is tried first only if its queue is no fuller; both are empty so A leads, defers, and B is built
    assert meta["picked"] == [a, b1] and meta["outcome"] == "ready"
    assert [r["status"] for r in meta["results"]] == ["deferred", "ready"]
    assert item_row(env, a)[:2] == ("discovered", 0)  # the deferral charged nothing


def test_run_builds_at_most_one_episode(env):
    first = add(env, LENNY, "AI evals", days_ago=1)
    second = add(env, GROWTH, "AI evals too", days_ago=1)
    meta = build.run_once(env.conn, env.cfg, make_deps(master=fake_master))
    assert len(meta["picked"]) == 1 and meta["outcome"] == "ready" and meta["cost"] == pytest.approx(0.006)
    assert sum(episode_row(env, i) is not None for i in (first, second)) == 1


def test_run_tries_the_next_candidate_after_an_off_brief_skip_but_stops_at_the_skip_limit(env):
    ids = [add(env, s, "AI evals " + s) for s in (LENNY, GROWTH, AIE)]
    skip = fake_model({"skip": True, "reason": "off brief"})
    meta = build.run_once(env.conn, env.cfg, make_deps(skip))
    assert [r["status"] for r in meta["results"]] == ["skipped", "skipped"]  # build.max_skips_per_run = 2
    assert meta["outcome"] == "skipped" and len(skip.calls) == 2
    assert sum(episode_row(env, i) is not None for i in ids) == 2


def test_run_stops_after_a_failed_episode_and_after_the_cap(env):
    add(env, LENNY, "AI evals")
    add(env, GROWTH, "AI evals too")
    meta = build.run_once(env.conn, env.cfg, make_deps(fake_model(lambda p: {"title": "bad"})))
    assert [r["status"] for r in meta["results"]] == ["failed"]  # one hour per retry, not a burst
    db.add_voice_cost(env.conn, TODAY, "B", "heygen", 1, 1, 0.14, 0)
    meta = build.run_once(env.conn, env.cfg, make_deps())
    assert [r["status"] for r in meta["results"]] == ["capped"]


def test_run_collects_first_and_goes_offline_when_every_feed_is_down(env):
    def down(url):
        raise OSError("network down")

    item = add(env, LENNY, "AI evals")
    deps = make_deps(master=fake_master)
    deps.fetch = down
    meta = build.run_once(env.conn, env.cfg, deps)
    assert "every feed failed" in meta["collect_error"] and meta["outcome"] == "offline" and meta["picked"] == []
    assert meta["feeds_ok"] == [] and set(meta["feeds_failed"]) == {s.name for s in env.cfg.sources}
    assert all("network down" in e for e in meta["feeds_failed"].values())
    assert episode_row(env, item) is None  # nothing was tried while offline


def test_run_with_nothing_to_do(env):
    meta = build.run_once(env.conn, env.cfg, make_deps())
    assert meta == {"picked": [], "outcome": "no candidates", "cost": 0.0, "results": [],
                    "assemble": "nothing_ready", "feeds_ok": [s.name for s in env.cfg.sources], "feeds_failed": {}}


# ---- main -------------------------------------------------------------------------------------------

def _main(env, tmp_path, monkeypatch, deps):
    monkeypatch.setattr(build, "real_deps", lambda cfg: deps)
    return build.main(["build", str(tmp_path / "config.toml")])


def run_rows(env, stage="build"):
    return env.conn.execute("select status, error, meta from runs where stage = ? order by id", (stage,)).fetchall()


def test_build_auth_error_exits_75_charges_nothing(env, tmp_path, monkeypatch):
    item = add(env, LENNY, "AI evals")

    def logged_out(prompt):
        return 1, "Failed to authenticate: OAuth session expired", ""

    logged_out.auth_patterns = ["Failed to authenticate"]
    heygen = fake_heygen()
    assert _main(env, tmp_path, monkeypatch, make_deps(logged_out, heygen)) == 75
    assert item_row(env, item)[1] == 0 and heygen[0].calls == [] and episode_row(env, item) is None
    (status, error, _meta), = run_rows(env)
    assert status == "failed" and error.startswith("LLMAuthError")  # what /health/show and the watcher read
    assert _meta is None  # no llm_ok: /health/llm must not read this run as an accepted token


def test_main_marks_llm_ok_when_claude_answered_even_if_the_script_was_bad(env, tmp_path, monkeypatch):
    add(env, LENNY, "AI evals")
    assert _main(env, tmp_path, monkeypatch, make_deps(fake_model(lambda p: {"title": "bad"}))) == 0
    assert json.loads(run_rows(env)[0][2])["llm_ok"] is True


def test_run_once_has_no_llm_ok_when_no_claude_call_was_made(env):
    meta = build.run_once(env.conn, env.cfg, make_deps(fake_model()))
    assert meta["outcome"] == "no candidates" and "llm_ok" not in meta


def test_main_records_the_run_with_picked_outcome_and_cost_and_exits_0_even_when_an_episode_fails(env, tmp_path,
                                                                                                    monkeypatch):
    item = add(env, LENNY, "AI evals")
    assert _main(env, tmp_path, monkeypatch, make_deps(fake_model(lambda p: {"title": "bad"}))) == 0
    (status, error, meta), = run_rows(env)
    meta = json.loads(meta)
    # nothing built and the outcome is a failure: the run is "failed" so /health/show and the watcher see it,
    # yet the exit code stays 0 (systemd must not mark the unit failed for an episode problem)
    assert status == "failed" and error.startswith("failed: no valid output") and meta["picked"] == [item]
    assert meta["outcome"] == "failed"


def test_main_good_run(env, tmp_path, monkeypatch):
    item = add(env, LENNY, "AI evals")
    assert _main(env, tmp_path, monkeypatch, make_deps(master=fake_master)) == 0
    meta = json.loads(run_rows(env)[0][2])
    assert meta["outcome"] == "ready" and meta["cost"] == pytest.approx(0.006) and meta["picked"] == [item]


def test_main_crash_is_recorded_and_exits_1(env, tmp_path, monkeypatch):
    add(env, LENNY, "AI evals")
    deps = make_deps()
    deps.fetch = lambda url: (_ for _ in ()).throw(RuntimeError("bug"))  # not an OSError: collect_all records it
    monkeypatch.setattr(brief, "load_brief", lambda *a, **k: (_ for _ in ()).throw(brief.BriefError("bad brief")))
    assert _main(env, tmp_path, monkeypatch, deps) == 1
    (status, error, _meta), = run_rows(env)
    assert status == "failed" and "BriefError" in error


def test_main_runs_upkeep_once_per_date(env, tmp_path, monkeypatch):
    for _ in range(2):  # two hourly runs on the same date
        assert _main(env, tmp_path, monkeypatch, make_deps()) == 0
    assert len(run_rows(env, "build")) == 2 and len(run_rows(env, "upkeep")) == 1
    assert run_rows(env, "upkeep")[0][0] == "ok"


def test_main_still_runs_upkeep_when_claude_is_logged_out(env, tmp_path, monkeypatch):
    add(env, LENNY, "AI evals")

    def logged_out(prompt):
        return 1, "Failed to authenticate", ""

    logged_out.auth_patterns = ["Failed to authenticate"]
    assert _main(env, tmp_path, monkeypatch, make_deps(logged_out)) == 75
    assert len(run_rows(env, "upkeep")) == 1


def test_real_deps_passes_the_token_from_the_secrets_file(tmp_path, monkeypatch):
    secrets_file = tmp_path / "s.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=tok-123\nOPENROUTER_API_KEY=k\n")
    cfg = load_config(write_config(tmp_path, {str(tmp_path / "secrets.env"): str(secrets_file)}))
    seen = {}
    monkeypatch.setattr(build.llm, "claude_runner",
                        lambda command, args, model, timeout, env=None, auth_patterns=(),
                                  unavailable_patterns=(): seen.update(env=env) or (lambda p: (0, "", "")))
    deps = build.real_deps(cfg)
    assert seen["env"] == {"CLAUDE_CODE_OAUTH_TOKEN": "tok-123"} and deps.prompts_dir == REPO / "prompts"
    assert deps.captions is not None and deps.listing is not None


def test_main_capped_with_nothing_built_is_a_failed_run_but_exit_0(env, tmp_path, monkeypatch):
    add(env, LENNY, "AI evals")
    db.add_voice_cost(env.conn, TODAY, "B", "heygen", 30000, 20, 0.14, 0)
    assert _main(env, tmp_path, monkeypatch, make_deps(master=fake_master)) == 0
    (status, error, meta), = run_rows(env)
    assert status == "failed" and error.startswith("capped:") and json.loads(meta)["outcome"] == "capped"


@pytest.mark.parametrize("deps_kw, outcome", [
    ({"captions": Captions(deferred=True)}, "deferred"),
    ({"model": fake_model({"skip": True, "reason": "off brief"})}, "skipped"),
    ({}, "no candidates"),
])
def test_main_deferred_skipped_and_idle_runs_stay_ok(env, tmp_path, monkeypatch, deps_kw, outcome):
    if outcome != "no candidates":
        add(env, AIE if outcome == "deferred" else LENNY, "AI evals")  # only YouTube captions can be deferred
    assert _main(env, tmp_path, monkeypatch, make_deps(**deps_kw)) == 0
    (status, error, meta), = run_rows(env)
    assert (status, error, json.loads(meta)["outcome"]) == ("ok", None, outcome)


def test_main_ready_run_stays_ok_even_if_a_later_candidate_would_have_failed(env, tmp_path, monkeypatch):
    add(env, LENNY, "AI evals")
    assert _main(env, tmp_path, monkeypatch, make_deps(master=fake_master)) == 0
    assert run_rows(env)[0][0] == "ok"


# ---- failures outside VoiceError/MixError ------------------------------------------------------------

@pytest.mark.parametrize("exc", [FileNotFoundError("ffmpeg"),
                                 subprocess.TimeoutExpired("ffmpeg", 1800)])
def test_build_one_charges_a_mastering_crash_instead_of_letting_it_escape(env, exc):
    item = add(env, LENNY, "t")

    def master(*a, **k):
        raise exc

    res = build.build_one(env.conn, env.cfg, env.b, item, make_deps(master=master))
    assert res["status"] == "failed" and res["stage"] == "master"
    assert item_row(env, item)[1] == 1 and episode_row(env, item) is None
    assert not list(env.cfg.paths.var_dir.rglob("*.wav"))
    assert env.conn.execute("select count(*) from voice_costs").fetchone() == (1,)  # HeyGen was billed: kept


# ---- cap accounting ---------------------------------------------------------------------------------

def test_the_estimate_includes_the_allowed_overrun(env):
    bd = env.cfg.build
    base = bd.max_minutes * bd.words_per_minute * bd.chars_per_word * env.cfg.voice.price_per_million_chars / 1e6
    assert build.estimate_usd(env.cfg) == pytest.approx(base * (1 + bd.max_over))


def test_unreadable_costs_are_counted_at_the_estimated_price_so_a_dead_endpoint_cannot_hide_spend(env, tmp_path):
    post, _ = fake_heygen()
    turns = valid_episode()["turns"]
    chars = voice.char_count(turns)
    build.render_heygen(env.conn, env.cfg, TODAY, "B", turns, tmp_path / "e.wav", post, lambda gid: None,
                        lambda s: None)
    billed, missing = env.conn.execute("select billed_usd, missing from voice_costs").fetchone()
    assert missing == len(turns)
    assert billed == pytest.approx(chars * env.cfg.voice.price_per_million_chars / 1e6)
    assert db.spend_today(env.conn, TODAY) == pytest.approx(billed)


def test_only_the_unreadable_turns_are_estimated(env, tmp_path):
    post, _ = fake_heygen()
    turns = [{"speaker": "A", "text": "x" * 100}, {"speaker": "B", "text": "y" * 300}]
    out = build.render_heygen(env.conn, env.cfg, TODAY, "B", turns, tmp_path / "e.wav", post,
                              lambda gid: 0.01 if gid == "gen-1" else None, lambda s: None)
    price = env.cfg.voice.price_per_million_chars
    assert out["billed_usd"] == pytest.approx(0.01 + 300 * price / 1e6) and out["billed_missing"] == 1


def test_run_records_per_feed_outcome(env, monkeypatch):
    bad = env.cfg.sources[0].name
    monkeypatch.setattr(collect, "collect_all", lambda *a, **k: {
        "sources": {s.name: [] for s in env.cfg.sources[1:]}, "errors": {bad: "E" * 999}})
    meta = build.run_once(env.conn, env.cfg, make_deps())
    assert meta["feeds_failed"] == {bad: "E" * env.cfg.collect.feed_error_chars}
    assert meta["feeds_ok"] == [s.name for s in env.cfg.sources[1:]] and "collect_error" not in meta


# ---- Claude unavailable / offline ---------------------------------------------------------------------

def hung_model(rc=124, out="", err="timed out after 600s"):
    calls = []

    def run(prompt):
        calls.append(prompt)
        return rc, out, err

    run.calls, run.auth_patterns = calls, []
    run.unavailable_patterns = ["Overloaded"]
    return run


@pytest.mark.parametrize("model", [hung_model(), hung_model(1, "", "API Error: Overloaded")])
def test_build_one_claude_unavailable_is_not_charged_and_not_llm_ok(env, model):
    item = add(env, LENNY, "t")
    heygen = fake_heygen()
    res = build.build_one(env.conn, env.cfg, env.b, item, make_deps(model, heygen))
    assert (res["status"], res["stage"], res["item_failed"], res["provider_down"]) == ("failed", "script", False, True)
    assert "llm_ok" not in res and item_row(env, item)[1] == 0 and len(model.calls) == 1 and heygen[0].calls == []


def test_run_stops_after_the_first_item_when_claude_is_down(env):
    add(env, LENNY, "a"), add(env, GROWTH, "b")
    model = hung_model()
    meta = build.run_once(env.conn, env.cfg, make_deps(model))
    assert len(meta["picked"]) == 1 and len(model.calls) == 1 and "llm_ok" not in meta
    assert build.run_failure(meta).startswith("failed: exit 124")


def test_every_feed_failing_means_offline_and_nothing_is_called(env, tmp_path, monkeypatch):
    item = add(env, LENNY, "t")
    model, heygen = fake_model(), fake_heygen()
    deps = make_deps(model, heygen)

    def dead(url):
        raise OSError("network unreachable")

    deps.fetch = dead
    assert _main(env, tmp_path, monkeypatch, deps) == 0
    (status, error, meta), = run_rows(env)
    meta = json.loads(meta)
    assert status == "failed" and error.startswith("offline: CollectError: every feed failed") and meta["outcome"] == "offline"
    assert meta["picked"] == [] and model.calls == [] and heygen[0].calls == [] and item_row(env, item)[1] == 0
    assert run_rows(env, "upkeep")  # upkeep still ran


# ---- the day's show is assembled at the start of a run, from show.assemble_at on --------------

def _deps_with_assemble(fn, now=NOW):
    d = make_deps(now=now)
    d.assemble = fn
    return d


def test_run_assembles_today_first_once_past_assemble_at(env):
    calls = []
    meta = build.run_once(env.conn, env.cfg, _deps_with_assemble(lambda c, cfg, date: calls.append(date) or {"show": "x"}))
    assert calls == [TODAY] and meta["assemble"] == "assembled"


def test_run_before_assemble_at_does_not_assemble(env):
    early = NOW.replace(hour=4, minute=59)
    meta = build.run_once(env.conn, env.cfg, _deps_with_assemble(lambda *a: pytest.fail("too early"), early))
    assert meta["assemble"] == "not_yet"


def test_run_with_nothing_ready_records_it_and_goes_on(env):
    meta = build.run_once(env.conn, env.cfg, _deps_with_assemble(lambda *a: {}))
    assert meta["assemble"] == "nothing_ready" and meta["outcome"] == "no candidates"


def test_run_skips_a_day_already_assembled(env):
    show = env.cfg.paths.var_dir / "show" / TODAY
    show.mkdir(parents=True)
    (show / "READY").write_text("{}")
    meta = build.run_once(env.conn, env.cfg, _deps_with_assemble(lambda *a: pytest.fail("already done")))
    assert meta["assemble"] == "already"


def test_assembly_failure_is_recorded_and_does_not_stop_the_run(env):
    def boom(*a):
        raise queue.QueueError("ffmpeg concat failed")

    meta = build.run_once(env.conn, env.cfg, _deps_with_assemble(boom))
    assert meta["assemble"].startswith("error: QueueError: ffmpeg concat failed") and "outcome" in meta


def test_llm_env_passes_the_token_or_an_api_key_and_nothing_else(env):
    f = env.cfg.voice.secrets_file
    f.parent.mkdir(parents=True, exist_ok=True)
    assert build.llm_env(env.cfg) is None
    f.write_text("OPENROUTER_API_KEY=or\nANTHROPIC_API_KEY=ak\n")
    assert build.llm_env(env.cfg) == {"ANTHROPIC_API_KEY": "ak"}
    f.write_text("OPENROUTER_API_KEY=or\nCLAUDE_CODE_OAUTH_TOKEN=tok\n")
    assert build.llm_env(env.cfg) == {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
