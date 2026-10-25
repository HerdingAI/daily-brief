import json

import numpy as np
import pytest
import soundfile as sf

from daily_brief import voice

SR = 44100  # voice.heygen_sample_rate: the WAV keeps HeyGen's own rate


def render(turns, out, synth, gap_s=0.1, change_gap_s=0.1, rms=-20.0, peak=-1.0, voices=None):
    return voice.render_turns(turns, voices or {"A": "weston", "B": "meredith"}, out, synth, SR, gap_s, change_gap_s,
                              rms, peak)


def synth_len(seconds_per_word):
    calls = []

    def synth(text, voice_name):
        calls.append((voice_name, text))
        return np.ones(int(len(text.split()) * seconds_per_word * SR), dtype=np.float32) * 0.1

    synth.calls = calls
    return synth


def test_renders_turns_in_script_order_with_right_voices_and_gaps(tmp_path):
    turns = [{"speaker": "A", "text": "one two"}, {"speaker": "B", "text": "three"}, {"speaker": "A", "text": "four"}]
    s = synth_len(0.5)
    out = tmp_path / "seg.wav"
    meta = render(turns, out, s, 0.25, 0.25)
    assert s.calls == [("weston", "one two"), ("meredith", "three"), ("weston", "four")]  # one request per turn, in order
    data, sr = sf.read(out)
    assert sr == SR
    expected = (2 * 0.5 + 0.5 + 0.5) + 3 * 0.25  # speech + one gap after each turn
    assert abs(len(data) / SR - expected) < 0.01
    assert meta["turns"] == 3 and abs(meta["seconds"] - expected) < 0.01


def test_a_turn_is_never_split_into_sentences(tmp_path):
    s = synth_len(0.1)
    render([{"speaker": "A", "text": "One. Two."}], tmp_path / "o.wav", s, 0.1, voices={"A": "a", "B": "b"})
    assert [t for _, t in s.calls] == ["One. Two."]


def test_audio_is_assembled_in_script_order(tmp_path):
    level = {"weston": 0.1, "meredith": -0.1}  # opposite signs survive the level match, so order is audible

    def synth(text, voice_name):
        return np.full(SR // 10, level[voice_name], dtype=np.float32)

    turns = [{"speaker": s, "text": f"turn {n}."} for n, s in enumerate("ABAB")]
    render(turns, tmp_path / "o.wav", synth, 0.1)
    data, _ = sf.read(tmp_path / "o.wav")
    starts = [round(i * (SR // 10 + SR // 10)) for i in range(4)]  # each turn: 0.1 s speech + 0.1 s gap
    assert [round(float(data[s + 10]), 1) for s in starts] == [0.1, -0.1, 0.1, -0.1]


def _rms_db(x):
    return 20 * np.log10(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def _turn_slices(data, n_turns, speech_s, gap_s):
    step = int((speech_s + gap_s) * SR)
    return [data[i * step:i * step + int(speech_s * SR)] for i in range(n_turns)]


def _sine(amp, seconds=0.5):
    return (amp * np.sin(2 * np.pi * 200 * np.arange(int(seconds * SR)) / SR)).astype(np.float32)


def test_turns_at_different_levels_come_out_within_1_db(tmp_path):
    level = {"weston": 0.02, "meredith": 0.6}  # 30 dB apart

    def synth(text, voice_name):
        return _sine(level[voice_name])

    turns = [{"speaker": s, "text": f"turn {n}."} for n, s in enumerate("ABAB")]
    render(turns, tmp_path / "o.wav", synth, 0.1, 0.1, rms=-20.0, peak=-1.0)
    data, _ = sf.read(tmp_path / "o.wav")
    levels = [_rms_db(t) for t in _turn_slices(data, 4, 0.5, 0.1)]
    assert max(levels) - min(levels) < 1.0 and abs(levels[0] - -20.0) < 0.5


def test_the_peak_ceiling_wins_over_the_rms_target(tmp_path):
    spiky = np.zeros(SR // 2, dtype=np.float32)
    spiky[::2000] = 0.9      # crest factor ~30 dB: reaching -20 dBFS RMS would put the peak far above 0 dBFS
    spiky[1] = 0.01
    render([{"speaker": "A", "text": "hi"}], tmp_path / "o.wav", lambda t, v: spiky, peak=-1.0)
    data, _ = sf.read(tmp_path / "o.wav")
    assert float(np.abs(data).max()) <= 10 ** (-1.0 / 20) + 1e-4


def test_silent_turns_are_left_alone(tmp_path):
    render([{"speaker": "A", "text": "hi"}], tmp_path / "o.wav", lambda t, v: np.zeros(SR // 10, dtype=np.float32))
    data, _ = sf.read(tmp_path / "o.wav")
    assert not np.isnan(data).any() and float(np.abs(data).max()) == 0.0


def test_the_gap_is_longer_on_a_speaker_change(tmp_path):
    turns = [{"speaker": s, "text": "x"} for s in "AAB"]  # continue, then change, then the last turn
    render(turns, tmp_path / "o.wav", lambda t, v: np.full(SR // 10, 0.1, dtype=np.float32), gap_s=0.2,
           change_gap_s=0.5)
    data, _ = sf.read(tmp_path / "o.wav")
    expected = 3 * 0.1 + 0.2 + 0.5 + 0.2  # A->A gap, A->B gap, trailing gap
    assert abs(len(data) / SR - expected) < 0.01


def test_unknown_speaker_fails_before_synthesis(tmp_path):
    s = synth_len(0.1)
    with pytest.raises(voice.VoiceError, match="speaker"):
        render([{"speaker": "A", "text": "x"}, {"speaker": "C", "text": "y"}], tmp_path / "o.wav", s,
               voices={"A": "a", "B": "b"})
    assert s.calls == []  # validated up front: no half-rendered output, no partial bill


def test_blank_turn_fails(tmp_path):
    with pytest.raises(voice.VoiceError, match="empty"):
        render([{"speaker": "A", "text": "  "}], tmp_path / "o.wav", synth_len(0.1), voices={"A": "a", "B": "b"})


def test_silent_synthesis_fails(tmp_path):
    with pytest.raises(voice.VoiceError, match="no audio"):
        render([{"speaker": "A", "text": "hello"}], tmp_path / "o.wav", lambda t, v: np.zeros(0, dtype=np.float32))


def test_synth_exception_becomes_voice_error_naming_the_voice(tmp_path):
    def broken(text, voice_name):
        raise ConnectionResetError(f"{voice_name} request reset")

    with pytest.raises(voice.VoiceError, match="weston.*request reset"):
        render([{"speaker": "A", "text": "hi"}], tmp_path / "o.wav", broken)
    assert not (tmp_path / "o.wav").exists()


def test_no_kokoro_or_fallback_engine_is_left_in_the_voice_module():
    assert not hasattr(voice, "make_kokoro") and not hasattr(voice, "render_with_fallback")


# ---- HeyGen Voice via OpenRouter ------------------------------------------------------------------------

def _pcm(seconds, rate=44100):
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * 220 * t) * 8000).astype("<i2").tobytes()


def test_heygen_synth_keeps_the_native_rate():
    ids = []
    synth = voice.heygen_synth(lambda body: (_pcm(1.0), "gen-1"), "m", ids.append)
    audio = synth("Hello.", "weston")
    assert audio.dtype == np.float32 and len(audio) == 44100  # 1 s of 44.1 kHz pcm in, 1 s of samples out
    assert not hasattr(voice, "_resample") and not hasattr(voice, "SAMPLE_RATE")
    assert float(np.abs(audio).max()) < 0.3  # s16le scaled to +-1, not left as raw ints
    assert ids == ["gen-1"]


def test_heygen_synth_sends_turn_voice_and_pcm():
    sent = []
    synth = voice.heygen_synth(lambda body: sent.append(body) or (_pcm(0.1), None), "heygen/x", lambda g: None)
    synth("Hello there.", "meredith")
    assert sent == [{"model": "heygen/x", "input": "Hello there.", "voice": "meredith", "response_format": "pcm"}]


@pytest.fixture
def openrouter_stub(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {"statuses": [], "calls": [], "auth": []}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            state["calls"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            state["auth"].append(self.headers["Authorization"])
            status = state["statuses"].pop(0) if state["statuses"] else 200
            body = _pcm(0.1) if status == 200 else b'{"error": "nope"}'
            self.send_response(status)
            self.send_header("x-generation-id", "gen-tts-abc")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(voice, "OPENROUTER_BASE", f"http://127.0.0.1:{httpd.server_address[1]}")
    yield state
    httpd.shutdown()


def test_heygen_poster_retries_5xx_then_succeeds(openrouter_stub):
    openrouter_stub["statuses"] = [503, 429]
    waits = []
    post = voice.heygen_poster("m", "k3y", 5, retries=2, retry_wait_s=7, sleep=waits.append)
    pcm, gen_id = post({"input": "hi", "voice": "weston", "response_format": "pcm"})
    assert pcm == _pcm(0.1) and gen_id == "gen-tts-abc"
    assert waits == [7, 7] and len(openrouter_stub["calls"]) == 3
    assert openrouter_stub["auth"][0] == "Bearer k3y" and openrouter_stub["calls"][0]["model"] == "m"


def test_heygen_poster_no_retry_on_400(openrouter_stub):
    openrouter_stub["statuses"] = [400]
    waits = []
    with pytest.raises(voice.VoiceError, match="HeyGen TTS HTTP 400"):
        voice.heygen_poster("m", "k", 5, retries=2, retry_wait_s=7, sleep=waits.append)({"input": "hi"})
    assert waits == [] and len(openrouter_stub["calls"]) == 1


def test_heygen_poster_gives_up_after_retries_on_5xx(openrouter_stub):
    openrouter_stub["statuses"] = [500, 500, 500, 500]
    with pytest.raises(voice.VoiceError, match="HTTP 500"):
        voice.heygen_poster("m", "k", 5, retries=2, retry_wait_s=0, sleep=lambda s: None)({"input": "hi"})
    assert len(openrouter_stub["calls"]) == 3


def test_billed_cost_retries_missing_ids():
    seen = {"b": 0}

    def fetch(gid):
        if gid == "b":
            seen["b"] += 1
            return None if seen["b"] < 3 else 0.002  # the stats land a few seconds late
        return 0.001

    waits = []
    assert voice.billed_cost(["a", "b"], fetch, attempts=6, wait_s=5, sleep=waits.append) == (0.003, 0)
    assert waits == [5, 5]


def test_billed_cost_reports_ids_that_never_appear():
    waits = []
    usd, missing = voice.billed_cost(["a", "b"], lambda g: 0.001 if g == "a" else None, 3, 5, waits.append)
    assert (usd, missing) == (0.001, 1) and waits == [5, 5]  # no sleep after the last attempt


def test_char_count_sums_stripped_turn_text():
    assert voice.char_count([{"text": " ab "}, {"text": "cde"}]) == 5


def test_billed_cost_prices_unreadable_ids_at_the_estimate_for_their_own_characters():
    usd, missing = voice.billed_cost(["a", "b"], lambda g: 0.001 if g == "a" else None, 2, 0, lambda s: None,
                                     id_chars={"a": 500, "b": 250}, price_per_million_chars=4.0)
    assert missing == 1 and usd == pytest.approx(0.001 + 250 * 4.0 / 1e6)
