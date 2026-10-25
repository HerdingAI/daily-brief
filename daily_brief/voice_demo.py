"""Voice one script file into an MP3 with the same voice + mix code the builder uses.

    python -m daily_brief.voice_demo demo/script.json demo/demo.mp3 [config.toml]

Needs OPENROUTER_API_KEY in the secrets file named by the config ([voice] secrets_file). It makes one
billed request per turn (about a cent for the bundled demo) and does not touch the database, the library
or the daily spend cap.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from daily_brief import mix, secrets, voice
from daily_brief.config import Config, load_config

MIN_DEMO_SECONDS = 5  # the builder's floor is much higher; a demo can be short


def build_demo(cfg: Config, script_path: Path, out: Path, synth: voice.Synth, say: Callable[[str], None] = print) -> dict:
    """Voice `script_path` ({"turns": [{"speaker": "A"|"B", "text": ...}]}) with `synth` into `out` (mp3)."""
    data = json.loads(Path(script_path).read_text())
    turns = data.get("turns")
    if not isinstance(turns, list) or not turns:
        raise voice.VoiceError("the script file needs a non-empty \"turns\" list")
    v, m = cfg.voice, cfg.mix
    say(f"voicing {len(turns)} turns, {voice.char_count(turns)} characters")
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "demo.wav"
        voice.render_turns(turns, v.by_speaker(), wav, synth, v.heygen_sample_rate, v.gap_seconds,
                           v.speaker_change_gap_seconds, v.turn_rms_dbfs, v.turn_peak_dbfs)
        return mix.master_segment(wav, out, MIN_DEMO_SECONDS, m.loudness_lufs, m.true_peak_db, m.bitrate)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: python -m daily_brief.voice_demo <script.json> <out.mp3> [config.toml]", file=sys.stderr)
        return 2
    cfg = load_config(argv[3] if len(argv) > 3 else "config.toml")
    v = cfg.voice
    try:
        key = secrets.require(v.secrets_file, "OPENROUTER_API_KEY")
    except secrets.SecretMissing as e:
        print(f"voice_demo: {e}", file=sys.stderr)
        return 1
    post = voice.heygen_poster(v.heygen_model, key, v.heygen_timeout_seconds, v.heygen_retries,
                               v.heygen_retry_wait_seconds)
    synth = voice.heygen_synth(post, v.heygen_model, lambda gen_id: None)
    try:
        res = build_demo(cfg, Path(argv[1]), Path(argv[2]), synth)
    except (voice.VoiceError, mix.MixError, OSError, ValueError) as e:
        print(f"voice_demo failed: {e}", file=sys.stderr)
        return 1
    print(f"wrote {argv[2]} ({res['seconds']} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
