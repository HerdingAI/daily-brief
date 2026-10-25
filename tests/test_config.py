import textwrap
from pathlib import Path

import pytest

from daily_brief.config import ConfigError, load_config

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.example.toml"


def write(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(body))
    return p


def test_repo_config_loads_and_is_typed():
    cfg = load_config(REPO_CONFIG)
    assert cfg.show.max_segment_minutes == 45
    assert cfg.show.segments == ["A", "B"]
    assert "~" not in str(cfg.paths.var_dir)  # user paths are expanded


def test_volume_range(tmp_path):
    body = REPO_CONFIG.read_text().replace("volume = 70", "volume = 150")
    with pytest.raises(ConfigError, match="player.volume"):
        load_config(write(tmp_path, body))


def test_unknown_segment_rejected(tmp_path):
    body = REPO_CONFIG.read_text().replace('segments = ["A", "B"]', 'segments = ["A", "Z"]')
    with pytest.raises(ConfigError, match="show.segments"):
        load_config(write(tmp_path, body))


def test_bool_is_not_a_number(tmp_path):
    body = REPO_CONFIG.read_text().replace("volume = 70", "volume = true")
    with pytest.raises(ConfigError, match="player.volume"):
        load_config(write(tmp_path, body))


def test_sources_loaded():
    cfg = load_config(REPO_CONFIG)
    assert cfg.sources[0].name == "Lenny's Podcast" and cfg.sources[0].backfill is True


def test_source_must_be_https(tmp_path):
    body = REPO_CONFIG.read_text().replace('url = "https://api.substack', 'url = "http://api.substack')
    with pytest.raises(ConfigError, match=r"sources\[0\]\.url"):
        load_config(write(tmp_path, body))


def test_source_bad_kind(tmp_path):
    body = REPO_CONFIG.read_text().replace('kind = "rss"', 'kind = "tiktok"')
    with pytest.raises(ConfigError, match=r"sources\[0\]\.kind"):
        load_config(write(tmp_path, body))


def test_voice_choice():
    v = load_config(REPO_CONFIG).voice
    assert v.by_speaker() == {"A": "weston", "B": "meredith"}  # HeyGen is the only engine; there is no backup voice
    assert v.gap_seconds > 0


def test_voice_keeps_heygens_native_rate_and_levels_turns():
    v = load_config(REPO_CONFIG).voice
    assert v.heygen_sample_rate == 44100 and v.speaker_change_gap_seconds > v.gap_seconds
    assert (v.turn_rms_dbfs, v.turn_peak_dbfs) == (-20.0, -1.0)


def test_youtube_is_transcript_only_by_default():
    assert load_config(REPO_CONFIG).youtube.audio_fallback is False


def test_transcript_api_timeout_is_configured():
    assert 0 < load_config(REPO_CONFIG).youtube.transcript_timeout_seconds <= 120


def test_alarm_clock_defaults_load():
    cfg = load_config(REPO_CONFIG)
    assert cfg.alarm_clock.window_minutes == 10
    assert cfg.alarm_clock.spotify_fade_seconds == 120
    assert cfg.alarm_clock.spotify_from_percent == 10
    assert cfg.alarm_clock.bridge_url == "http://127.0.0.1:8766"
    assert cfg.alarm_clock.bridge_timeout_seconds == 10


@pytest.mark.parametrize("key,bad", [("window_minutes", 0), ("window_minutes", 500),
                                      ("spotify_from_percent", 101), ("spotify_fade_seconds", -1)])
def test_alarm_clock_rejects_out_of_range(tmp_path, key, bad):
    import re
    body = re.sub(rf"(?m)^{key} = \S+", f"{key} = {bad}", REPO_CONFIG.read_text(), count=1)
    with pytest.raises(ConfigError, match=f"alarm_clock.{key}"):
        load_config(write(tmp_path, body))


def test_heygen_defaults_and_cap_load():
    v = load_config(REPO_CONFIG).voice
    assert v.heygen_model == "heygen/heygen-voice-1-turbo" and v.heygen_sample_rate == 44100
    assert v.daily_cap_usd == 0.15 and v.price_per_million_chars == 4.0


def test_voice_cap_must_be_positive(tmp_path):
    body = __import__("re").sub(r'(?m)^daily_cap_usd = \S+', 'daily_cap_usd = 0', REPO_CONFIG.read_text())
    with pytest.raises(ConfigError, match="daily_cap_usd"):
        load_config(write(tmp_path, body))


def test_build_defaults_load():
    b = load_config(REPO_CONFIG).build
    assert (b.per_run, b.max_attempts, b.max_new_per_source_per_day) == (1, 2, 1)
    assert (b.min_minutes, b.max_minutes, b.max_over, b.words_per_minute, b.chars_per_word) == (4, 20, 0.15, 150, 6)
    assert b.max_turn_words == 90 and b.max_same_speaker_run == 3 and "insane" in b.hype_words


@pytest.mark.parametrize("old,new,key", [
    ("per_run = 1", "per_run = 0", "build.per_run"),
    ("min_minutes = 4", "min_minutes = 30", "build.min_minutes"),
    ("words_per_minute = 150", "words_per_minute = 0", "build.words_per_minute"),
    ("max_over = 0.15", "max_over = -0.1", "build.max_over"),
    ("max_turn_words = 90", "max_turn_words = 0", "build.max_turn_words"),
    ('hype_words = ["game-changer"', 'hype_words = ["", "game-changer"', "build.hype_words"),
    ("gap_seconds = 0.35", "gap_seconds = -1", "voice.gap_seconds"),
    ("speaker_change_gap_seconds = 0.45", "speaker_change_gap_seconds = -1", "voice.gap_seconds"),
    ("turn_peak_dbfs = -1.0", "turn_peak_dbfs = 3.0", "voice.turn_rms_dbfs"),
    ("turn_rms_dbfs = -20.0", "turn_rms_dbfs = -0.5", "voice.turn_rms_dbfs"),
    ("max_share_of_source = 0.4", "max_share_of_source = 0", "build.max_share_of_source"),
    ('enabled_segments = ["A", "B"]', 'enabled_segments = ["Z"]', "pipeline.enabled_segments"),
])
def test_build_validation_names_the_key(tmp_path, old, new, key):
    text = REPO_CONFIG.read_text()
    assert old in text
    with pytest.raises(ConfigError, match=key.replace(".", r"\.")):
        load_config(write(tmp_path, text.replace(old, new, 1)))


def test_removed_night_machinery_is_gone_from_the_config():
    text = REPO_CONFIG.read_text()
    for table in ("[night]", "[preflight]", "[prefetch]", "[editor]", "[backcatalog]"):
        assert table not in text
    for key in ("backup_enabled", "fallback_items", "extract_max_attempts", "backlog_cap_per_source",
                "stale_after_days", "backup_host", "sentence_gap_seconds", "cpu_threads = 16\n\n[mix]"):
        assert key not in text


def test_queue_and_retention_defaults_load():
    cfg = load_config(REPO_CONFIG)
    assert cfg.show.max_segment_minutes == 45 and cfg.pipeline.keep_library_days == 30
    assert cfg.llm.health_cache_seconds == 600


def test_a_missing_new_key_names_the_key(tmp_path):
    body = REPO_CONFIG.read_text().replace("max_segment_minutes = 45", "")
    with pytest.raises(ConfigError, match=r"show\.max_segment_minutes"):
        load_config(write(tmp_path, body))


@pytest.mark.parametrize("old,new,key", [
    ("max_segment_minutes = 45", "max_segment_minutes = 10", "show.max_segment_minutes"),   # < one longest episode
    ("keep_library_days = 30", "keep_library_days = 0", "pipeline.keep_library_days"),
    ("health_cache_seconds = 600", "health_cache_seconds = -1", "llm.health_cache_seconds"),
    ("max_segment_minutes = 45", "max_segment_minutes = true", "show.max_segment_minutes"),
])
def test_queue_validation_names_the_key(tmp_path, old, new, key):
    with pytest.raises(ConfigError, match=key.replace(".", r"\.")):
        load_config(write(tmp_path, REPO_CONFIG.read_text().replace(old, new, 1)))


def test_dead_music_alarm_and_window_config_is_gone():
    text = REPO_CONFIG.read_text()
    assert "[alarm]" not in text and "music_src" not in text
    for key in ("cache_minutes", "fade_in_seconds", "min_track_seconds", "no_repeat_days", 'start = "06:00"', "08:00"):
        assert key not in text
    assert "[alarm_clock]" in text      # the configurable alarm stays


def test_write_config_keeps_every_written_path_under_tmp_path(tmp_path):
    """The fixture config must never point a path a test WRITES at the real home (a test once created the real
    ~/.config/daily_brief/secrets.env). Only read-only executables may stay in the real home."""
    from tests.conftest import write_config
    cfg = load_config(write_config(tmp_path))
    written = {"paths.var_dir": cfg.paths.var_dir, "paths.archive_dir": cfg.paths.archive_dir,
               "voice.secrets_file": cfg.voice.secrets_file, "player.ipc_socket": cfg.player.ipc_socket}
    for name, path in written.items():
        assert path.is_relative_to(tmp_path), f"{name} = {path} escapes tmp_path"
    # and nothing else in the written config text names the real home (the executables are the allowed exceptions)
    text = (tmp_path / "config.toml").read_text()
    stray = [ln for ln in text.splitlines() if "\"~/" in ln
             and "~/.local/bin/" not in ln]
    assert not stray, stray
