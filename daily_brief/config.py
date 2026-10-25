"""Load and validate config.toml into typed, frozen dataclasses.

Every problem raises ConfigError naming the dotted key, so a bad edit fails loudly at load
time instead of at 06:00.
"""

from __future__ import annotations

import datetime as dt
import tomllib
from dataclasses import dataclass
from pathlib import Path

KNOWN_SEGMENTS = {"A", "B"}



class ConfigError(ValueError):
    """Raised when config.toml is missing a key or holds an invalid value."""


@dataclass(frozen=True)
class Paths:
    var_dir: Path
    archive_dir: Path


@dataclass(frozen=True)
class Show:
    segments: list[str]
    max_segment_minutes: int         # the queue stops adding episodes to a morning's segment past this
    min_segment_seconds: int         # a ready episode shorter than this is treated as unplayable
    assemble_at: dt.time             # local time of day from which the builder assembles today's show


@dataclass(frozen=True)
class Feed:
    title: str
    description: str
    items: int                       # newest assembled shows listed in /feed.xml
    base_url: str                    # public URL of this API for enclosure links; "" = derive from the request


@dataclass(frozen=True)
class BriefCfg:
    path: Path
    tv_forbidden_terms: list[str]


@dataclass(frozen=True)
class Collect:
    new_window_days: int
    min_episode_seconds: int
    http_timeout_seconds: int
    user_agent: str
    feed_alert_hours: int
    feed_alert_lookback_hours: int
    feed_error_chars: int


@dataclass(frozen=True)
class Source:
    segment: str
    kind: str
    name: str
    url: str
    backfill: bool


@dataclass(frozen=True)
class Transcribe:
    model: str
    compute_type: str
    cpu_threads: int
    beam_size: int
    vad_filter: bool
    marker_every_seconds: int
    max_attempts: int


@dataclass(frozen=True)
class YouTube:
    ytdlp: Path
    listing_limit: int
    listing_timeout_seconds: int
    audio_fallback: bool
    transcript_timeout_seconds: int


@dataclass(frozen=True)
class LLM:
    command: Path
    args: list[str]
    model: str
    timeout_seconds: int
    max_attempts: int
    auth_error_patterns: list[str]  # output that means "logged out", never retried
    unavailable_patterns: list[str]  # failed-exit output that means "Claude is down / limited", never charged to an item
    health_cache_seconds: int       # /health/llm re-asks `claude auth status` at most this often


@dataclass(frozen=True)
class Voice:
    heygen_model: str
    heygen_sample_rate: int          # Hz of the raw pcm the API returns
    heygen_timeout_seconds: int
    heygen_retries: int
    heygen_retry_wait_seconds: float
    price_per_million_chars: float   # billed price, used for the pre-flight estimate against the cap
    daily_cap_usd: float
    cost_readback_attempts: int
    cost_readback_wait_seconds: float
    secrets_file: Path
    host_a: str
    host_b: str
    gap_seconds: float
    speaker_change_gap_seconds: float
    turn_rms_dbfs: float             # per-turn gain-match target
    turn_peak_dbfs: float            # peak ceiling for that gain

    def by_speaker(self) -> dict[str, str]:
        return {"A": self.host_a, "B": self.host_b}


@dataclass(frozen=True)
class Mix:
    loudness_lufs: float
    true_peak_db: float
    bitrate: str
    intro: Path | None
    outro: Path | None


@dataclass(frozen=True)
class Build:
    interval_minutes: int            # the builder container starts a build run at every multiple of this (wall clock)
    run_timeout_minutes: int         # a build run still going after this is killed; the loop goes on
    per_run: int                     # episodes finished per hourly run
    max_attempts: int                # charged failures before an item is set aside
    max_new_per_source_per_day: int  # per creator, so a prolific channel can't crowd out the rest
    max_skips_per_run: int           # off-brief skips (each costs a Claude call) tolerated in one run
    min_minutes: float               # a script shorter than this has nothing worth an episode
    max_minutes: float
    max_share_of_source: float       # per-episode max = this share of the transcript's spoken length, clamped
    max_over: float                  # the script may run this fraction over max_minutes
    words_per_minute: int
    chars_per_word: float            # sizes the voice-cost estimate before the script exists
    max_turn_words: int
    max_same_speaker_run: int
    hype_words: list[str]
    history_days: int                # covered topics + feedback the prompt sees


@dataclass(frozen=True)
class Qa:
    max_wer: float                   # audio_qa: word error rate (whisper vs script) above this fails the check


@dataclass(frozen=True)
class Pipeline:
    enabled_segments: list[str]
    keep_work_days: int
    keep_show_days: int
    archive_keep_days: int
    keep_library_days: int           # built episodes stay this long after their morning (or skip) before the files go


@dataclass(frozen=True)
class Api:
    host: str
    port: int


@dataclass(frozen=True)
class Config:
    paths: Paths
    show: Show
    feed: Feed
    brief: BriefCfg
    collect: Collect
    sources: list[Source]
    transcribe: Transcribe
    youtube: YouTube
    llm: LLM
    voice: Voice
    mix: Mix
    build: Build
    qa: Qa
    pipeline: Pipeline
    api: Api


def _get(raw: dict, dotted: str, kind: type):
    node = raw
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"missing required key {dotted}")
        node = node[part]
    # bool is a subclass of int; never accept it where a number is expected
    if kind in (int, float) and isinstance(node, bool):
        raise ConfigError(f"{dotted} must be a number, got {node!r}")
    if kind is float and isinstance(node, int):
        node = float(node)
    if not isinstance(node, kind):
        raise ConfigError(f"{dotted} must be {kind.__name__}, got {node!r}")
    return node


def _path(raw: dict, dotted: str) -> Path:
    return Path(_get(raw, dotted, str)).expanduser()


def _hhmm(raw: dict, dotted: str) -> dt.time:
    value = _get(raw, dotted, str)
    try:
        if len(value) != 5:  # fromisoformat also takes "0500" and "05:00:30"
            raise ValueError(value)
        return dt.time.fromisoformat(value)
    except ValueError as e:
        raise ConfigError(f"{dotted} must be HH:MM (24 h, local time), got {value!r}") from e


def _optional_path(raw: dict, dotted: str) -> Path | None:
    value = _get(raw, dotted, str)  # the key must exist; an empty string means "none"
    return Path(value).expanduser() if value else None


def _sources(raw: dict) -> list[Source]:
    items = raw.get("sources")
    if not isinstance(items, list) or not items:
        raise ConfigError("missing required key sources (at least one [[sources]] table)")
    out = []
    for i, item in enumerate(items):
        wrapped = {"s": item}
        src = Source(
            segment=_get(wrapped, "s.segment", str),
            kind=_get(wrapped, "s.kind", str),
            name=_get(wrapped, "s.name", str),
            url=_get(wrapped, "s.url", str),
            backfill=_get(wrapped, "s.backfill", bool),
        )
        if src.segment not in KNOWN_SEGMENTS:
            raise ConfigError(f"sources[{i}].segment must be one of {sorted(KNOWN_SEGMENTS)}")
        if src.kind not in {"rss", "youtube"}:
            raise ConfigError(f"sources[{i}].kind must be rss or youtube")
        if not src.url.startswith("https://"):
            raise ConfigError(f"sources[{i}].url must be https")
        out.append(src)
    urls = [s.url for s in out]
    if len(urls) != len(set(urls)):
        raise ConfigError("sources: duplicate url")
    return out


def load_config(path: str | Path) -> Config:
    try:
        raw = tomllib.loads(Path(path).read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e

    cfg = Config(
        paths=Paths(
            var_dir=_path(raw, "paths.var_dir"),
            archive_dir=_path(raw, "paths.archive_dir"),
        ),
        show=Show(
            segments=_get(raw, "show.segments", list),
            max_segment_minutes=_get(raw, "show.max_segment_minutes", int),
            min_segment_seconds=_get(raw, "show.min_segment_seconds", int),
            assemble_at=_hhmm(raw, "show.assemble_at"),
        ),
        feed=Feed(
            title=_get(raw, "feed.title", str),
            description=_get(raw, "feed.description", str),
            items=_get(raw, "feed.items", int),
            base_url=_get(raw, "feed.base_url", str).rstrip("/"),
        ),
        brief=BriefCfg(
            path=_path(raw, "brief.path"),
            tv_forbidden_terms=_get(raw, "brief.tv_forbidden_terms", list),
        ),
        collect=Collect(
            new_window_days=_get(raw, "collect.new_window_days", int),
            min_episode_seconds=_get(raw, "collect.min_episode_seconds", int),
            http_timeout_seconds=_get(raw, "collect.http_timeout_seconds", int),
            user_agent=_get(raw, "collect.user_agent", str),
            feed_alert_hours=_get(raw, "collect.feed_alert_hours", int),
            feed_alert_lookback_hours=_get(raw, "collect.feed_alert_lookback_hours", int),
            feed_error_chars=_get(raw, "collect.feed_error_chars", int),
        ),
        sources=_sources(raw),
        transcribe=Transcribe(
            model=_get(raw, "transcribe.model", str),
            compute_type=_get(raw, "transcribe.compute_type", str),
            cpu_threads=_get(raw, "transcribe.cpu_threads", int),
            beam_size=_get(raw, "transcribe.beam_size", int),
            vad_filter=_get(raw, "transcribe.vad_filter", bool),
            marker_every_seconds=_get(raw, "transcribe.marker_every_seconds", int),
            max_attempts=_get(raw, "transcribe.max_attempts", int),
        ),
        youtube=YouTube(ytdlp=_path(raw, "youtube.ytdlp"), listing_limit=_get(raw, "youtube.listing_limit", int),
                        listing_timeout_seconds=_get(raw, "youtube.listing_timeout_seconds", int),
                        audio_fallback=_get(raw, "youtube.audio_fallback", bool),
                        transcript_timeout_seconds=_get(raw, "youtube.transcript_timeout_seconds", int)),
        llm=LLM(
            command=_path(raw, "llm.command"),
            args=_get(raw, "llm.args", list),
            model=_get(raw, "llm.model", str),
            timeout_seconds=_get(raw, "llm.timeout_seconds", int),
            max_attempts=_get(raw, "llm.max_attempts", int),
            auth_error_patterns=_get(raw, "llm.auth_error_patterns", list),
            unavailable_patterns=_get(raw, "llm.unavailable_patterns", list),
            health_cache_seconds=_get(raw, "llm.health_cache_seconds", int),
        ),
        voice=Voice(
            heygen_model=_get(raw, "voice.heygen_model", str),
            heygen_sample_rate=_get(raw, "voice.heygen_sample_rate", int),
            heygen_timeout_seconds=_get(raw, "voice.heygen_timeout_seconds", int),
            heygen_retries=_get(raw, "voice.heygen_retries", int),
            heygen_retry_wait_seconds=_get(raw, "voice.heygen_retry_wait_seconds", float),
            price_per_million_chars=_get(raw, "voice.price_per_million_chars", float),
            daily_cap_usd=_get(raw, "voice.daily_cap_usd", float),
            cost_readback_attempts=_get(raw, "voice.cost_readback_attempts", int),
            cost_readback_wait_seconds=_get(raw, "voice.cost_readback_wait_seconds", float),
            secrets_file=_path(raw, "voice.secrets_file"),
            host_a=_get(raw, "voice.host_a", str),
            host_b=_get(raw, "voice.host_b", str),
            gap_seconds=_get(raw, "voice.gap_seconds", float),
            speaker_change_gap_seconds=_get(raw, "voice.speaker_change_gap_seconds", float),
            turn_rms_dbfs=_get(raw, "voice.turn_rms_dbfs", float),
            turn_peak_dbfs=_get(raw, "voice.turn_peak_dbfs", float),
        ),
        mix=Mix(
            loudness_lufs=_get(raw, "mix.loudness_lufs", float),
            true_peak_db=_get(raw, "mix.true_peak_db", float),
            bitrate=_get(raw, "mix.bitrate", str),
            intro=_optional_path(raw, "mix.intro"),
            outro=_optional_path(raw, "mix.outro"),
        ),
        build=Build(
            interval_minutes=_get(raw, "build.interval_minutes", int),
            run_timeout_minutes=_get(raw, "build.run_timeout_minutes", int),
            per_run=_get(raw, "build.per_run", int),
            max_attempts=_get(raw, "build.max_attempts", int),
            max_new_per_source_per_day=_get(raw, "build.max_new_per_source_per_day", int),
            max_skips_per_run=_get(raw, "build.max_skips_per_run", int),
            min_minutes=_get(raw, "build.min_minutes", float),
            max_minutes=_get(raw, "build.max_minutes", float),
            max_share_of_source=_get(raw, "build.max_share_of_source", float),
            max_over=_get(raw, "build.max_over", float),
            words_per_minute=_get(raw, "build.words_per_minute", int),
            chars_per_word=_get(raw, "build.chars_per_word", float),
            max_turn_words=_get(raw, "build.max_turn_words", int),
            max_same_speaker_run=_get(raw, "build.max_same_speaker_run", int),
            hype_words=_get(raw, "build.hype_words", list),
            history_days=_get(raw, "build.history_days", int),
        ),
        qa=Qa(max_wer=_get(raw, "qa.max_wer", float)),
        pipeline=Pipeline(
            enabled_segments=_get(raw, "pipeline.enabled_segments", list),
            keep_work_days=_get(raw, "pipeline.keep_work_days", int),
            keep_show_days=_get(raw, "pipeline.keep_show_days", int),
            archive_keep_days=_get(raw, "pipeline.archive_keep_days", int),
            keep_library_days=_get(raw, "pipeline.keep_library_days", int),
        ),
        api=Api(host=_get(raw, "api.host", str), port=_get(raw, "api.port", int)),
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if cfg.feed.items < 1:
        raise ConfigError("feed.items must be >= 1")
    if cfg.feed.base_url and not cfg.feed.base_url.startswith(("http://", "https://")):
        raise ConfigError("feed.base_url must be empty or start with http:// or https://")
    bad = [s for s in cfg.show.segments if s not in KNOWN_SEGMENTS]
    if bad or not cfg.show.segments:
        raise ConfigError(f"show.segments must be a non-empty subset of {sorted(KNOWN_SEGMENTS)}, got {bad}")
    bad_enabled = [s for s in cfg.pipeline.enabled_segments if s not in cfg.show.segments]
    if bad_enabled:
        raise ConfigError(f"pipeline.enabled_segments {bad_enabled} are not in show.segments")
    longest = cfg.build.max_minutes * (1 + cfg.build.max_over)
    if cfg.show.max_segment_minutes < longest:
        raise ConfigError(f"show.max_segment_minutes must hold one longest episode ({longest:.1f} min), "
                          f"got {cfg.show.max_segment_minutes}")
    if cfg.pipeline.keep_library_days < 1:
        raise ConfigError("pipeline.keep_library_days must be >= 1")
    if cfg.llm.health_cache_seconds < 0:
        raise ConfigError("llm.health_cache_seconds must be >= 0")
    bd = cfg.build
    if min(bd.interval_minutes, bd.run_timeout_minutes) < 1:
        raise ConfigError("build.interval_minutes and build.run_timeout_minutes must be >= 1")
    if min(bd.per_run, bd.max_attempts, bd.max_new_per_source_per_day, bd.max_skips_per_run) < 1:
        raise ConfigError("build.per_run, max_attempts, max_new_per_source_per_day and max_skips_per_run must be >= 1")
    if not 0 < bd.min_minutes < bd.max_minutes:
        raise ConfigError("build.min_minutes must be > 0 and < build.max_minutes")
    if not 0 < bd.max_share_of_source <= 1:
        raise ConfigError("build.max_share_of_source must be > 0 and <= 1")
    if bd.max_over < 0:
        raise ConfigError("build.max_over must be >= 0")
    if bd.words_per_minute < 1 or bd.chars_per_word <= 0:
        raise ConfigError("build.words_per_minute and build.chars_per_word must be > 0")
    if bd.max_turn_words < 1 or bd.max_same_speaker_run < 1:
        raise ConfigError("build.max_turn_words and build.max_same_speaker_run must be >= 1")
    if not 0 <= cfg.qa.max_wer <= 1:
        raise ConfigError("qa.max_wer must be 0..1")
    if bd.history_days < 0:
        raise ConfigError("build.history_days must be >= 0")
    if not all(isinstance(x, str) and x for x in bd.hype_words):
        raise ConfigError("build.hype_words must be a list of non-empty strings")
    vc = cfg.voice
    if not vc.heygen_model or vc.heygen_sample_rate < 8000 or vc.heygen_timeout_seconds < 1 \
            or vc.heygen_retries < 0 or vc.heygen_retry_wait_seconds < 0 or vc.gap_seconds < 0 \
            or vc.speaker_change_gap_seconds < 0:
        raise ConfigError("voice.heygen_*: model non-empty, sample_rate >= 8000, timeout >= 1, retries and waits >= 0; "
                          "voice.gap_seconds and speaker_change_gap_seconds >= 0")
    if not vc.turn_rms_dbfs < 0 or not vc.turn_rms_dbfs <= vc.turn_peak_dbfs <= 0:
        raise ConfigError("voice.turn_rms_dbfs must be < 0 and <= voice.turn_peak_dbfs, which must be <= 0")
    if vc.price_per_million_chars <= 0 or vc.daily_cap_usd <= 0 \
            or vc.cost_readback_attempts < 1 or vc.cost_readback_wait_seconds < 0:
        raise ConfigError("voice.price_per_million_chars and daily_cap_usd must be > 0; "
                          "cost_readback_attempts >= 1; cost_readback_wait_seconds >= 0")
    if not cfg.pipeline.enabled_segments:
        raise ConfigError("pipeline.enabled_segments must not be empty")
    if not cfg.llm.auth_error_patterns or not all(isinstance(x, str) and x for x in cfg.llm.auth_error_patterns):
        raise ConfigError("llm.auth_error_patterns must be a non-empty list of non-empty strings")
    if not all(isinstance(x, str) and x for x in cfg.llm.unavailable_patterns):
        raise ConfigError("llm.unavailable_patterns must be a list of non-empty strings")
