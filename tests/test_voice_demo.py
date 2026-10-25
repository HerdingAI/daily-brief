import json
import shutil

import numpy as np
import pytest

from daily_brief import script, voice, voice_demo
from daily_brief.config import load_config
from tests.conftest import REPO, write_config

DEMO = REPO / "demo" / "script.json"


def test_demo_script_is_a_valid_two_host_script():
    d = json.loads(DEMO.read_text())
    rules = script.Rules(words_per_minute=150, min_minutes=1, max_minutes=20, max_over=0.15, max_turn_words=90,
                         max_same_speaker_run=3, hype_words=["game-changer", "revolutionary"],
                         priority_ids=frozenset({"p1"}))
    script.validate(d, rules)
    assert {t["speaker"] for t in d["turns"]} == {"A", "B"} and 8 <= len(d["turns"]) <= 14


def test_demo_script_makes_no_real_world_claims_about_people_or_shows():
    text = DEMO.read_text().lower()
    assert "http" not in text


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_build_demo_voices_every_turn_with_the_given_synth(tmp_path):
    cfg = load_config(write_config(tmp_path))
    calls = []

    def synth(text, voice_name):  # a quiet tone per turn, no network
        calls.append((voice_name, text))
        t = np.arange(int(0.6 * cfg.voice.heygen_sample_rate)) / cfg.voice.heygen_sample_rate
        return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    out = tmp_path / "demo.mp3"
    res = voice_demo.build_demo(cfg, DEMO, out, synth, say=lambda s: None)
    turns = json.loads(DEMO.read_text())["turns"]
    assert len(calls) == len(turns) and out.is_file() and res["seconds"] >= voice_demo.MIN_DEMO_SECONDS
    assert not (tmp_path / "var" / "brief.db").exists()  # the demo never touches the database


def test_build_demo_rejects_a_script_without_turns(tmp_path):
    cfg = load_config(write_config(tmp_path))
    bad = tmp_path / "s.json"
    bad.write_text("{}")
    with pytest.raises(voice.VoiceError):
        voice_demo.build_demo(cfg, bad, tmp_path / "o.mp3", lambda t, v: np.zeros(1, np.float32), say=lambda s: None)


def test_main_without_a_key_fails_cleanly(tmp_path, capsys):
    cfg_path = write_config(tmp_path)  # secrets_file points into tmp_path and does not exist
    assert voice_demo.main(["voice_demo", str(DEMO), str(tmp_path / "o.mp3"), str(cfg_path)]) == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_demo_floor_is_lower_than_the_min_segment():
    assert voice_demo.MIN_DEMO_SECONDS < load_config(REPO / "config.example.toml").show.min_segment_seconds
