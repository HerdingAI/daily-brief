import shutil
import subprocess

import pytest

from daily_brief import mix
from daily_brief.mix import probe_duration

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def tone(path, seconds, freq=440, rate=24000):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={seconds}:sample_rate={rate}", str(path)], check=True)
    return path


def test_master_segment_normalises_and_keeps_length(tmp_path):
    wav = tone(tmp_path / "seg.wav", 6)
    out = tmp_path / "show" / "B.mp3"
    meta = mix.master_segment(wav, out, min_s=3, lufs=-16, true_peak=-1.5, bitrate="128k")
    assert out.exists() and abs(probe_duration(out) - 6) < 0.3
    assert abs(meta["seconds"] - 6) < 0.3


def test_stings_are_added(tmp_path):
    wav = tone(tmp_path / "seg.wav", 5)
    intro = tone(tmp_path / "in.wav", 2, 880, rate=44100)  # different sample rate on purpose
    outro = tone(tmp_path / "out.wav", 1, 660)
    out = tmp_path / "A.mp3"
    mix.master_segment(wav, out, min_s=3, lufs=-16, true_peak=-1.5, bitrate="128k", intro=intro, outro=outro)
    assert abs(probe_duration(out) - 8) < 0.4


def test_too_short_is_rejected_and_nothing_left_behind(tmp_path):
    wav = tone(tmp_path / "seg.wav", 2)
    out = tmp_path / "B.mp3"
    with pytest.raises(mix.MixError, match="too short"):
        mix.master_segment(wav, out, min_s=60, lufs=-16, true_peak=-1.5, bitrate="128k")
    assert not out.exists() and not list(tmp_path.glob("*.tmp*"))

