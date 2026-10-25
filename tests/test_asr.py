import json
import subprocess
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from daily_brief import asr, voice


@pytest.fixture
def tone(tmp_path):
    """5 seconds of sine as an mp3 (real ffmpeg)."""
    out = tmp_path / "tone.mp3"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=5", str(out)], check=True)
    return out


def answer(text, cost=0.0001, segs=((0.5, "a"), (1.0, "b"))):
    return {"text": text, "usage": {"seconds": 2, "cost": cost},
            "segments": [{"id": i, "start": s, "end": s + 0.4, "text": t} for i, (s, t) in enumerate(segs)]}


def test_segments_are_offset_by_each_chunks_start_and_cost_is_summed(tone):
    posted = []

    def post(data, name):
        posted.append((name, len(data)))
        return answer(f"chunk {len(posted)}", cost=0.0002 * len(posted))

    client = asr.Transcriber(post, chunk_seconds=2)
    segs = client(tone)
    assert len(posted) == 3 and [n for n, _ in posted] == ["chunk00000.mp3", "chunk00001.mp3", "chunk00002.mp3"]
    starts = [s.start for s in segs]
    assert starts == pytest.approx([0.5, 1.0, 2.5, 3.0, 4.5, 5.0], abs=0.1)  # chunk starts 0, 2, 4 (+ muxer rounding)
    assert [s.text for s in segs] == ["a", "b"] * 3
    assert client.usage.requests == 3 and client.usage.missing == 0
    assert client.usage.cost == pytest.approx(0.0002 + 0.0004 + 0.0006)
    assert not list(tone.parent.glob("asr-*"))  # chunk files are cleaned up


def test_a_short_file_is_one_chunk_and_a_missing_cost_is_counted(tone):
    client = asr.Transcriber(lambda d, n: {"text": "hi", "segments": [{"start": 1.5, "text": "hi"}]}, 600)
    segs = client(tone)
    assert [(s.start, s.text) for s in segs] == [(1.5, "hi")]
    assert (client.usage.requests, client.usage.missing, client.usage.cost) == (1, 1, 0.0)


def test_a_later_failure_keeps_the_bill_of_the_chunks_before_it(tone):
    calls = []

    def post(data, name):
        calls.append(name)
        if len(calls) == 2:
            raise asr.TranscriptionError("ASR HTTP 400: bad")
        return answer("x", cost=0.0005)

    client = asr.Transcriber(post, 2)
    with pytest.raises(asr.TranscriptionError):
        client(tone)
    assert client.usage.requests == 1 and client.usage.cost == pytest.approx(0.0005)


def test_answer_without_text_is_an_item_failure(tone):
    with pytest.raises(asr.TranscriptionError, match="no text"):
        asr.Transcriber(lambda d, n: {"usage": {"cost": 0.1}}, 600)(tone)


def test_unreadable_audio_is_an_item_failure(tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"not audio")
    with pytest.raises(asr.TranscriptionError, match="ffmpeg"):
        asr.Transcriber(lambda d, n: answer("x"), 600)(bad)


def test_usage_take_resets():
    u = asr.Usage(0.5, 1, 2)
    assert u.take() == asr.Usage(0.5, 1, 2) and u == asr.Usage()


# ---- the HTTP poster against a local server ----------------------------------------------------------

@pytest.fixture
def server(monkeypatch):
    state = {"replies": [], "seen": []}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["seen"].append((self.path, self.headers["Authorization"], self.headers["Content-Type"], body))
            code, payload = state["replies"].pop(0)
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(voice, "OPENROUTER_BASE", f"http://127.0.0.1:{srv.server_port}/api/v1")
    yield state
    srv.shutdown()


def poster(retries=2, sleeps=None):
    return asr.asr_poster("m/asr", "k3y", 5, retries, 7, (sleeps if sleeps is not None else []).append)


def test_post_sends_multipart_with_model_file_and_format(server):
    server["replies"] = [(200, answer("ok"))]
    assert poster()(b"AUDIO", "c.mp3")["text"] == "ok"
    path, auth, ctype, body = server["seen"][0]
    assert path == "/api/v1/audio/transcriptions" and auth == "Bearer k3y" and ctype.startswith("multipart/form-data")
    assert b'name="model"\r\n\r\nm/asr' in body and b'name="response_format"\r\n\r\nverbose_json' in body
    assert b'name="file"; filename="c.mp3"' in body and b"AUDIO" in body


def test_429_then_200_is_retried_after_the_wait(server):
    server["replies"] = [(429, {"error": "slow down"}), (503, {}), (200, answer("ok"))]
    sleeps = []
    assert poster(2, sleeps)(b"A", "c.mp3")["text"] == "ok" and sleeps == [7, 7]


def test_transient_errors_beyond_the_retries_are_unavailable(server):
    server["replies"] = [(500, {})] * 3
    sleeps = []
    with pytest.raises(asr.TranscriptionUnavailable, match="HTTP 500"):
        poster(2, sleeps)(b"A", "c.mp3")
    assert len(server["seen"]) == 3 and len(sleeps) == 2


@pytest.mark.parametrize("code", [401, 402, 403])
def test_account_errors_are_unavailable_at_once(server, code):
    server["replies"] = [(code, {"error": "no"})]
    sleeps = []
    with pytest.raises(asr.TranscriptionUnavailable, match=f"HTTP {code}"):
        poster(3, sleeps)(b"A", "c.mp3")
    assert len(server["seen"]) == 1 and sleeps == []


def test_400_and_malformed_answers_are_item_failures_not_unavailable(server):
    server["replies"] = [(400, {"error": "bad"}), (200, b"<html>"), (200, b"[1]")]
    for _ in range(3):
        with pytest.raises(asr.TranscriptionError) as e:
            poster()(b"A", "c.mp3")
        assert not isinstance(e.value, asr.TranscriptionUnavailable)
    assert len(server["seen"]) == 3


def test_unreachable_server_is_unavailable_after_the_retries(monkeypatch):
    monkeypatch.setattr(voice, "OPENROUTER_BASE", "http://127.0.0.1:1/api/v1")
    sleeps = []
    with pytest.raises(asr.TranscriptionUnavailable, match="unreachable"):
        poster(1, sleeps)(b"A", "c.mp3")
    assert sleeps == [7]
    assert issubclass(urllib.error.URLError, OSError)
