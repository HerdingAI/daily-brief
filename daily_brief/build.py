"""Hourly episode builder (v2): ONE episode becomes ONE finished MP3 per run.

collect -> pick -> transcript -> cap check -> one Claude call (prompts/episode.md) -> HeyGen -> loudness ->
var/library/<item_id>/episode.mp3 + an `episodes` row. A failure of any kind costs one hour, not a show: the
next run tries again, and a ready queue is the only backup there is.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from daily_brief import brief, collect, db, ledger, llm, mix, rank, script, secrets, transcribe, upkeep, voice
from daily_brief.brief import Brief
from daily_brief.config import Config, load_config

EXIT_AUTH = 75  # EX_TEMPFAIL: Claude is logged out; the run produced nothing and no item is at fault
TOKEN_KEY = "CLAUDE_CODE_OAUTH_TOKEN"


@dataclass
class Deps:
    """Everything with side effects, injectable for tests."""
    runner: llm.Runner
    download: transcribe.Download
    make_whisper: Callable[[], transcribe.Whisper]
    now: Callable[[], dt.datetime]
    prompts_dir: Path
    fetch: Callable[[str], bytes]                                       # feed fetcher
    listing: Callable[[collect.SourceSpec], list[collect.Episode]] | None = None  # YouTube channel fallback
    captions: Callable[[str], str | None] | None = None                 # YouTube transcript sources, tried in order
    heygen_post: Callable[[dict], tuple[bytes, str | None]] | None = None  # injectable transports (tests);
    heygen_fetch: Callable[[str], float | None] | None = None              # None = the real OpenRouter calls
    sleep: Callable[[float], None] = time.sleep
    master: Callable[..., dict] = mix.master_segment
    _whisper: transcribe.Whisper | None = field(default=None, repr=False)

    def whisper(self) -> transcribe.Whisper:
        if self._whisper is None:
            self._whisper = self.make_whisper()  # load the model only if something needs transcribing
        return self._whisper

    def prompt(self, name: str) -> str:
        return (self.prompts_dir / f"{name}.md").read_text()


def make_captions(cfg: Config) -> Callable[[str], str | None]:
    """The YouTube transcript sources, each behind its own rate-limit breaker."""
    return transcribe.first_captions(
        transcribe.CaptionFetcher(functools.partial(transcribe.transcript_api_captions,
                                                    every_s=cfg.transcribe.marker_every_seconds,
                                                    timeout_s=cfg.youtube.transcript_timeout_seconds)),
        transcribe.CaptionFetcher(functools.partial(transcribe.ytdlp_captions,
                                                    workdir=cfg.paths.var_dir / "work" / "captions",
                                                    ytdlp=str(cfg.youtube.ytdlp))))


def llm_env(cfg: Config) -> dict[str, str] | None:
    """The environment Claude Code runs with: a systemd user unit has no login session, so it authenticates from
    the long-lived token in the secrets file. None when there is no token (a logged-in desktop session)."""
    token = secrets.load(cfg.voice.secrets_file).get(TOKEN_KEY)
    return {TOKEN_KEY: token} if token else None


def real_deps(cfg: Config) -> Deps:
    t, y = cfg.transcribe, cfg.youtube
    return Deps(
        runner=llm.claude_runner(str(cfg.llm.command), cfg.llm.args, cfg.llm.model, cfg.llm.timeout_seconds,
                                 env=llm_env(cfg),
                                 auth_patterns=cfg.llm.auth_error_patterns,
                                 unavailable_patterns=cfg.llm.unavailable_patterns),
        download=functools.partial(transcribe.download_any, timeout_s=cfg.collect.http_timeout_seconds * 5,
                                   user_agent=cfg.collect.user_agent, ytdlp=str(y.ytdlp)),
        make_whisper=lambda: transcribe.make_whisper(t.model, t.compute_type, t.cpu_threads, t.beam_size,
                                                     t.vad_filter),
        now=lambda: dt.datetime.now().astimezone(),
        prompts_dir=Path(__file__).resolve().parent.parent / "prompts",
        fetch=functools.partial(collect.http_fetch, timeout_s=cfg.collect.http_timeout_seconds,
                                user_agent=cfg.collect.user_agent),
        listing=functools.partial(collect.ytdlp_channel_listing, ytdlp=str(y.ytdlp), limit=y.listing_limit,
                                  timeout_s=y.listing_timeout_seconds),
        captions=make_captions(cfg),
    )


# ---- pick -------------------------------------------------------------------------------------------

def _waiting(conn: sqlite3.Connection, segment: str) -> int:
    """Ready episodes of a segment that no morning has taken yet."""
    return conn.execute("select count(*) from episodes where segment = ? and status = 'ready' "
                        "and assigned_date is null", (segment,)).fetchone()[0]


def _built_today(conn: sqlite3.Connection, source_id: int, day: str) -> int:
    """New (not back-catalog) episodes of this source already decided today, ready or skipped."""
    return conn.execute("select count(*) from episodes e join items i on i.id = e.item_id "
                        "where i.source_id = ? and i.is_backcatalog = 0 and substr(e.built_at, 1, 10) = ?",
                        (source_id, day)).fetchone()[0]


def pick(conn: sqlite3.Connection, cfg: Config, b: Brief, today: dt.date) -> list[int]:
    """Candidate items in the order to try them.

    Segments with the fewest waiting episodes go first, so the emptiest queue is filled first. Within a
    segment: new items (inside collect.new_window_days, no episodes row, not failed), best keyword match first
    and at most build.max_new_per_source_per_day per creator per day; then, only when the segment has nothing
    waiting at all, one back-catalog pick. No LLM: the single Claude call per episode makes the real judgement."""
    day = today.isoformat()
    cutoff = (today - dt.timedelta(days=cfg.collect.new_window_days)).isoformat()
    wanted, avoid = rank.brief_stems(b)
    waiting = {seg: _waiting(conn, seg) for seg in cfg.pipeline.enabled_segments}
    out: list[int] = []
    for seg in sorted(waiting, key=waiting.get):  # sorted is stable: config order breaks ties
        scored: dict[int, list[tuple[int, str, int]]] = {}
        for item_id, source_id, title, published in conn.execute(
                "select i.id, i.source_id, i.title, i.published_at from items i join sources s on s.id = i.source_id "
                "where s.segment = ? and s.enabled = 1 and i.published_at >= ? and i.status != 'failed' "
                "and not exists (select 1 from episodes e where e.item_id = i.id)", (seg, cutoff)):
            scored.setdefault(source_id, []).append((rank.title_score(title, wanted, avoid), published, item_id))
        picked: list[tuple[int, str, int]] = []
        for source_id, rows in scored.items():
            room = cfg.build.max_new_per_source_per_day - _built_today(conn, source_id, day)
            picked += sorted(rows, reverse=True)[:max(room, 0)]  # best match first, newer breaks ties
        out += [item_id for _, _, item_id in sorted(picked, reverse=True)]
        if waiting[seg] == 0:
            out += rank.pick_backcatalog(conn, b, 1, exclude=frozenset(out), segment=seg)
    return out


# ---- one episode ------------------------------------------------------------------------------------

def estimate_usd(cfg: Config) -> float:
    """Worst-case voice cost of one episode, known before the script exists. Deliberately the global max_minutes,
    not the per-episode one: the cap check runs before the transcript is fetched (a transcription can cost
    minutes of CPU), so the source length is not known yet, and the conservative figure can only over-reserve.
    Includes max_over: the validator lets a script run that far past max_minutes."""
    bd = cfg.build
    return (bd.max_minutes * (1 + bd.max_over) * bd.words_per_minute * bd.chars_per_word
            * cfg.voice.price_per_million_chars / 1e6)


def episode_max_minutes(cfg: Config, transcript: str) -> float:
    """Longest this episode may run: build.max_share_of_source of the source's spoken length, clamped to
    min/max_minutes. A digest of an 18-minute talk must not run 14 minutes. Rounded to 0.1 min so the prompt
    reads naturally and the validator's limit is exactly what the prompt says."""
    bd = cfg.build
    share = transcribe.spoken_words(transcript) * bd.max_share_of_source / bd.words_per_minute
    return round(min(max(share, bd.min_minutes), bd.max_minutes), 1)


def budget_text(cfg: Config, max_minutes: float) -> str:
    bd = cfg.build
    return (f"Spoken length: at least {bd.min_minutes:g} and at most {max_minutes:g} minutes, at about "
            f"{bd.words_per_minute} words per minute (at most {bd.max_over:.0%} slack over the maximum).\n"
            f"Each turn: at most {bd.max_turn_words} words.\n"
            f"At most {bd.max_same_speaker_run} turns in a row from the same speaker.\n"
            f"Banned hype words: {', '.join(bd.hype_words)}.")


def voice_transport(cfg: Config, deps: Deps) -> tuple[Callable, Callable]:
    """(post, fetch) for HeyGen: the injected fakes, or the real OpenRouter calls. A missing key raises
    VoiceNotConfigured before any Claude call is made."""
    v = cfg.voice
    post, fetch = deps.heygen_post, deps.heygen_fetch
    if post is None or fetch is None:
        try:
            key = secrets.require(v.secrets_file, "OPENROUTER_API_KEY")
        except secrets.SecretMissing as e:
            raise voice.VoiceNotConfigured(str(e)) from e
        post = post or voice.heygen_poster(v.heygen_model, key, v.heygen_timeout_seconds, v.heygen_retries,
                                           v.heygen_retry_wait_seconds, deps.sleep)
        fetch = fetch or voice.generation_fetcher(key, v.heygen_timeout_seconds)
    return post, fetch


def render_heygen(conn: sqlite3.Connection, cfg: Config, date: str, segment: str, turns: list[dict], out: Path,
                  post: Callable, fetch: Callable, sleep: Callable[[float], None]) -> dict:
    v = cfg.voice
    chars = voice.char_count(turns)
    est = chars * v.price_per_million_chars / 1e6
    spent = db.spend_today(conn, date)
    if spent + est > v.daily_cap_usd:  # decided before any request, so a capped script costs no voice
        raise voice.DailyCapReached(f"daily cap: ${spent:.3f} spent + ${est:.3f} est > ${v.daily_cap_usd:.2f}")
    gen_ids: list[str] = []
    id_chars: dict[str, int] = {}  # characters behind each generation, to price one whose cost can't be read back
    untracked_chars = untracked = requests = 0

    def counting_post(body: dict):
        nonlocal requests, untracked_chars, untracked
        requests += 1
        pcm, gen_id = post(body)
        if gen_id:
            id_chars[gen_id] = len(body["input"])
        else:  # billed but nothing to read the cost back with
            untracked_chars += len(body["input"])
            untracked += 1
        return pcm, gen_id

    synth = voice.heygen_synth(counting_post, v.heygen_model, gen_ids.append)
    try:
        meta = voice.render_turns(turns, v.by_speaker(), out, synth, v.heygen_sample_rate, v.gap_seconds,
                                  v.speaker_change_gap_seconds, v.turn_rms_dbfs, v.turn_peak_dbfs)
    finally:
        # also on failure: the turns billed before a mid-episode error still count against the cap
        # cost accounting: what could not be read back is charged at the estimated price (see voice.billed_cost),
        # so voice_costs.billed_usd is never an under-count and the cap still trips when /generation is down
        billed, missing = voice.billed_cost(gen_ids, fetch, v.cost_readback_attempts, v.cost_readback_wait_seconds,
                                            sleep, id_chars, v.price_per_million_chars)
        billed += untracked_chars * v.price_per_million_chars / 1e6
        missing += untracked
        db.add_voice_cost(conn, date, segment, "heygen", chars, requests, billed, missing)
    return meta | {"chars": chars, "requests": requests, "billed_usd": round(billed, 6), "billed_missing": missing}


def _charge(conn: sqlite3.Connection, cfg: Config, item_id: int, stage: str, error: str) -> dict:
    """An episode's own failure: one attempt charged; at build.max_attempts the item is set aside for good."""
    transcribe.record_failure(conn, item_id, f"{stage}: {error}", cfg.build.max_attempts)
    status = conn.execute("select status from items where id = ?", (item_id,)).fetchone()[0]
    return {"status": "failed", "item_id": item_id, "stage": stage, "error": error[:300],
            "item_failed": status == "failed"}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def _cached_script(path: Path, rules: script.Rules) -> tuple[dict, dict] | None:
    """(script, llm meta) from a script.json left by an earlier failed voice/master step, or None when it is
    missing, unparsable, a skip, or no longer valid under today's rules (then Claude is called afresh)."""
    try:
        c = json.loads(path.read_text())
        data, meta = c["script"], c["llm"]
        if data.get("skip") is True:
            return None
        script.validate(data, rules)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):  # ValueError covers bad JSON and rejections
        return None
    return data, meta


def build_one(conn: sqlite3.Connection, cfg: Config, b: Brief, item_id: int, deps: Deps) -> dict:
    """One item -> status ready | skipped | deferred | capped | failed (plus detail). LLMAuthError propagates:
    a logged-out Claude is nobody's fault but the login, so nothing is charged and the run stops."""
    now = deps.now()
    date = now.date().isoformat()
    title, published, backcat, creator, segment = conn.execute(
        "select i.title, i.published_at, i.is_backcatalog, s.name, s.segment from items i "
        "join sources s on s.id = i.source_id where i.id = ?", (item_id,)).fetchone()
    base = {"item_id": item_id, "segment": segment}

    est = estimate_usd(cfg)
    spent = db.spend_today(conn, date)
    if spent + est > cfg.voice.daily_cap_usd:  # before the transcript, the Claude call and any voice request
        return base | {"status": "capped", "reason": f"${spent:.3f} spent + ${est:.3f} est > "
                                                     f"${cfg.voice.daily_cap_usd:.2f}"}
    try:
        post, fetch = voice_transport(cfg, deps)
    except voice.VoiceNotConfigured as e:
        return base | {"status": "failed", "stage": "voice", "error": str(e), "item_failed": False}

    var = cfg.paths.var_dir
    work = var / "work" / date / str(item_id)
    t = cfg.transcribe
    stats = transcribe.transcribe_items(
        conn, [item_id], work, var / "transcripts", deps.download, lambda p: deps.whisper()(p), t.marker_every_seconds,
        t.max_attempts, captions=deps.captions, youtube_audio_fallback=cfg.youtube.audio_fallback)
    row = conn.execute("select path from transcripts where item_id = ?", (item_id,)).fetchone()
    if stats["deferred"]:  # YouTube could not be asked: no attempt charged, the item stays first in line
        reason = conn.execute("select fail_reason from items where id = ?", (item_id,)).fetchone()[0]
        return base | {"status": "deferred", "reason": reason}
    if row is None:  # transcribe.record_failure already charged this one
        status, reason = conn.execute("select status, fail_reason from items where id = ?", (item_id,)).fetchone()
        return base | {"status": "failed", "stage": "transcript", "error": reason, "item_failed": status == "failed"}

    bd = cfg.build
    transcript = Path(row[0]).read_text()
    max_minutes = episode_max_minutes(cfg, transcript)
    rules = script.Rules.from_config(bd, b.priority_ids()).with_max(max_minutes)
    inputs = {
        "brief": b.prompt_text(),
        "history": ledger.editor_context(conn, date, bd.history_days),
        "episode": json.dumps({"creator": creator, "title": title, "published": published,
                               "is_backcatalog": bool(backcat)}),
        "budget_minutes": budget_text(cfg, max_minutes),
        "transcript": transcript,
    }
    lib = var / "library" / str(item_id)
    cache = lib / "script.json"
    cached = _cached_script(cache, rules)
    if cached:  # a paid script whose voice/master step failed: reuse it, no Claude call (and no proof of the token)
        data, llm_meta = cached
        base["script_cached"] = True
    else:
        try:
            data, llm_meta = llm.call_json(deps.prompt("episode"), inputs, lambda d: script.validate(d, rules),
                                           deps.runner, cfg.llm.max_attempts)
        except llm.LLMAuthError:
            raise
        except llm.LLMUnavailable as e:  # outage / limit / hang: not the item's fault, and no proof of the token
            return base | {"status": "failed", "stage": "script", "error": str(e)[:300], "item_failed": False,
                           "provider_down": True}
        except llm.LLMError as e:  # Claude answered (the token was accepted) but nothing valid came back
            base["llm_ok"] = True
            return base | _charge(conn, cfg, item_id, "script", str(e))
        base["llm_ok"] = True  # every result from here on proves the token works, whatever happens to voice/mix
    built_at = now.isoformat(timespec="seconds")

    if data.get("skip") is True:  # off-brief: remembered, never picked again, no voice spent
        conn.execute("insert into episodes(item_id, segment, status, why, built_at) values (?, ?, 'skipped', ?, ?)",
                     (item_id, segment, data["reason"], built_at))
        return base | {"status": "skipped", "reason": data["reason"]}

    if not cached:  # before any voice is bought, so the next hour need not pay Claude again
        lib.mkdir(parents=True, exist_ok=True)
        _write_json(cache, {"script": data, "llm": llm_meta, "max_minutes": max_minutes})
    wav = work / "episode.wav"
    try:
        voiced = render_heygen(conn, cfg, date, segment, data["turns"], wav, post, fetch, deps.sleep)
        mx = cfg.mix
        mastered = deps.master(wav, lib / "episode.mp3", cfg.player.min_segment_seconds, mx.loudness_lufs,
                               mx.true_peak_db, mx.bitrate, mx.intro, mx.outro)
    except voice.DailyCapReached as e:  # the script was longer than the estimate; try again tomorrow's money
        return base | {"status": "capped", "reason": str(e)}
    except voice.VoiceUnavailable as e:  # provider outage / account trouble: the episode is fine, so no attempt is
        # charged; the run stops (run_once) and is recorded failed. Partial spend was booked by render_heygen.
        return base | {"status": "failed", "stage": "voice", "error": str(e)[:300], "item_failed": False,
                       "provider_down": True}
    except (voice.VoiceError, mix.MixError) as e:
        return base | _charge(conn, cfg, item_id, "voice", str(e))
    # not ours to raise but still the episode's bill: HeyGen has been paid, so without this the next hour would
    # repeat a paid Claude call and a paid voice render forever (no ffmpeg, a hung ffmpeg, a full disk)
    except (OSError, subprocess.SubprocessError) as e:
        return base | _charge(conn, cfg, item_id, "master", f"{type(e).__name__}: {e}")
    finally:
        wav.unlink(missing_ok=True)

    cache.unlink(missing_ok=True)  # episode.json carries the turns now; library pruning removes the rest
    _write_json(lib / "episode.json", {
        "item_id": item_id, "creator": creator, "source_title": title, "published": published, "segment": segment,
        "title": data["title"], "why": data["why"], "chapters": data["chapters"], "so_what": data["so_what"],
        "turns": data["turns"], "built_at": built_at, "llm": llm_meta, "voice": voiced, "mix": mastered})
    conn.execute(
        "insert into episodes(item_id, segment, status, title, why, chapters, so_what, path, seconds, chars, "
        "billed_usd, built_at) values (?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, segment, data["title"], data["why"], json.dumps(data["chapters"]), data["so_what"],
         str(lib / "episode.mp3"), mastered["seconds"], voiced["chars"], voiced["billed_usd"], built_at))
    return base | {"status": "ready", "title": data["title"], "seconds": mastered["seconds"],
                   "billed_usd": voiced["billed_usd"]}


# ---- the hourly run ---------------------------------------------------------------------------------

def run_once(conn: sqlite3.Connection, cfg: Config, deps: Deps) -> dict:
    """Collect, then try candidates until one episode is built. Returns the run's meta for the runs table."""
    now = deps.now()
    specs = [collect.SourceSpec(**s.__dict__) for s in cfg.sources]
    c = cfg.collect
    meta: dict = {"picked": [], "outcome": "no candidates", "cost": 0.0, "results": []}
    try:
        got = collect.collect_all(conn, specs, deps.fetch, now, c.new_window_days, c.min_episode_seconds, deps.listing)
        failed = got["errors"] or {}
        ok = list(got["sources"])
    # a feed outage must not stop the builder: items already collected can still be built
    except Exception as e:  # noqa: BLE001
        meta["collect_error"] = f"{type(e).__name__}: {e}"
        failed = getattr(e, "errors", None)  # every feed failed: CollectError carries the per-feed errors
        ok = []
    # per-feed outcome for /health/show feeds_failing: builds succeed from the backlog while a dead feed stays silent
    if failed is not None:
        meta["feeds_ok"] = ok
        meta["feeds_failed"] = {n: err[:c.feed_error_chars] for n, err in failed.items()}
    b = brief.load_brief(brief.effective_path(cfg), cfg.brief.tv_forbidden_terms)  # invalid = loud failure
    if failed and not ok:  # every feed failed: the network is down, so Claude/voice would only hang or fail
        meta["outcome"] = "offline"
        return meta
    built = skips = 0
    for item_id in pick(conn, cfg, b, now.date()):
        result = build_one(conn, cfg, b, item_id, deps)
        status = result["status"]
        meta["picked"].append(item_id)
        if result.pop("llm_ok", False):  # /health/llm: a call got past auth this run (`claude auth status` cannot tell)
            meta["llm_ok"] = True
        meta["results"].append(result)
        meta["outcome"] = status
        meta["cost"] = round(meta["cost"] + result.get("billed_usd", 0.0), 6)
        if status == "ready":
            built += 1
            if built >= cfg.build.per_run:
                break
        elif status == "skipped":  # off-brief: costs a Claude call but made no episode, so try the next candidate
            skips += 1
            if skips >= cfg.build.max_skips_per_run:
                break
        elif result.get("provider_down"):  # one outage must not be "discovered" once per candidate
            break
        elif status == "deferred" or (status == "failed" and result.get("stage") == "transcript"):
            continue  # YouTube could not be asked / no transcript: no Claude call was spent, try the next candidate
        else:  # capped, or an episode failed after Claude/voice work: stop and let the next hour retry
            break
    return meta


def run_failure(meta: dict) -> str | None:
    """Why a run that built nothing counts as failed: its outcome was `failed`, `capped` or `offline`. None for a run that
    built an episode, and for deferred / skipped / no-candidates runs (nobody is at fault; the next hour retries)."""
    if meta["outcome"] == "offline":
        return f"offline: {meta.get('collect_error', '')}"[:300]
    if meta["outcome"] not in ("failed", "capped") or any(r["status"] == "ready" for r in meta["results"]):
        return None
    last = meta["results"][-1]
    return f"{meta['outcome']}: {last.get('reason') or last.get('error') or ''}"[:300]


def main(argv: list[str]) -> int:
    cfg = load_config(argv[1] if len(argv) > 1 else "config.toml")
    conn = db.connect(cfg.paths.var_dir / "brief.db")
    date = dt.datetime.now().astimezone().date().isoformat()
    run = db.start_run(conn, date, "build")
    code = 0
    try:
        meta = run_once(conn, cfg, real_deps(cfg))
        # an episode problem is the exit code's business only for systemd (0), but the run is "failed" when nothing
        # was built and the last outcome was a failure or the cap: that is what failed_in_a_row and the watcher count
        failure = run_failure(meta)
        db.finish_run(conn, run, "failed" if failure else "ok", error=failure, meta=meta)
        print(f"build: {meta['outcome']} {meta['picked']} cost ${meta['cost']}")
    except llm.LLMAuthError as e:
        db.finish_run(conn, run, "failed", error=f"LLMAuthError: {e}")
        print(f"build: Claude is logged out: {e}", file=sys.stderr)
        code = EXIT_AUTH
    # top-level boundary: record why the run died so /health/show and the watcher can see it, then fail loudly
    except Exception as e:  # noqa: BLE001
        db.finish_run(conn, run, "failed", error=f"{type(e).__name__}: {e}")
        print(f"build FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        code = 1
    upkeep_meta = upkeep.run_if_due(conn, cfg, date)  # once per date, whatever happened above
    if upkeep_meta:
        print(f"upkeep: {upkeep_meta['status']} {upkeep_meta.get('errors') or ''}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
