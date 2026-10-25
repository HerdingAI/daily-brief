import datetime as dt
from pathlib import Path

import pytest

from daily_brief import collect, db, transcribe

FX = Path(__file__).parent / "fixtures"
FEED = (FX / "example_youtube_feed.xml").read_bytes()
NOW = dt.datetime(2026, 9, 26, 12, tzinfo=dt.UTC)
EXAMPLE_CH = collect.SourceSpec(segment="A", kind="youtube", name="Example Channel", url="https://www.youtube.com/feeds/videos.xml?channel_id=X",
                          backfill=False)


def test_parse_youtube_feed_skips_shorts():
    eps = collect.parse_youtube_feed(FEED)
    ids = [e.guid for e in eps]
    assert "exampleVid1" in ids and "exampleShort" not in ids  # the /shorts/ entry is dropped
    e = eps[0]
    assert e.audio_url == e.url == "https://www.youtube.com/watch?v=exampleVid1"
    assert e.published_at.year == 2026 and e.duration_s is None


def test_collect_dispatches_on_kind(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    sid = collect.sync_sources(conn, [EXAMPLE_CH])[EXAMPLE_CH.url]
    stats = collect.collect_source(conn, sid, EXAMPLE_CH, lambda u: FEED, NOW, 7, 900)
    assert stats["new"] >= 1 and stats["backcatalog"] == 0  # backfill off: old videos ignored


def test_vtt_to_transcript_dedupes_rolling_captions():
    text = transcribe.vtt_to_transcript((FX / "example_captions.vtt").read_text(), every_s=30)
    assert text.startswith("[00:00] Today we look at a small idea, and why it matters more than it seems. The more we test it,")
    assert text.count("why it matters") == 1


# ---- captions-first with a 429 circuit breaker ------------------------------------------------
@pytest.fixture
def yt(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    conn.execute("insert into sources(id,segment,kind,url,name) values (1,'A','youtube','https://f','Example Channel')")
    ids = [conn.execute("insert into items(source_id,guid,title,audio_url,url) values (1,?,?,?,?)",
                        (g, g, f"https://www.youtube.com/watch?v={g}", f"https://www.youtube.com/watch?v={g}")).lastrowid
           for g in ("v1", "v2", "v3")]
    return conn, ids, tmp_path


def audio_ok(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"m4a")


def test_captions_used_when_available(yt):
    conn, ids, tmp = yt
    whisper_calls = []
    res = transcribe.transcribe_items(
        conn, ids[:1], tmp / "w", tmp / "t", download=audio_ok,
        whisper=lambda p: whisper_calls.append(p) or [], every_s=30, max_attempts=3,
        captions=transcribe.CaptionFetcher(lambda url: "[00:00] from captions"))
    assert res["ok"] == 1 and whisper_calls == []
    assert conn.execute("select method from transcripts").fetchone()[0] == "captions"


def test_rate_limit_trips_breaker_and_falls_back_to_audio(yt):
    conn, ids, tmp = yt
    tried = []

    def fetch(url):
        tried.append(url)
        raise transcribe.RateLimited("HTTP Error 429")

    from types import SimpleNamespace as S
    cf = transcribe.CaptionFetcher(fetch)
    res = transcribe.transcribe_items(conn, ids, tmp / "w", tmp / "t", download=audio_ok,
                                      whisper=lambda p: [S(start=0, text="from whisper")], every_s=30, max_attempts=3,
                                      captions=cf, youtube_audio_fallback=True)
    assert res["ok"] == 3 and len(tried) == 1 and cf.tripped  # one 429, then no more caption requests
    assert {r[0] for r in conn.execute("select method from transcripts")} == {"whisper"}


def test_captions_never_tried_for_podcast_audio(yt):
    conn, _, tmp = yt
    rss_item = conn.execute("insert into items(source_id,guid,title,audio_url) values (1,'p','p','https://x/ep.mp3')").lastrowid
    tried = []
    from types import SimpleNamespace as S
    transcribe.transcribe_items(conn, [rss_item], tmp / "w", tmp / "t", download=audio_ok,
                                whisper=lambda p: [S(start=0, text="w")], every_s=30, max_attempts=3,
                                captions=transcribe.CaptionFetcher(lambda u: tried.append(u)))
    assert tried == []


# ---- transcript-only YouTube (policy: transcript only, never the whole video) ----

def test_youtube_without_transcript_fails_and_never_downloads(yt):
    conn, ids, tmp = yt
    downloads = []
    res = transcribe.transcribe_items(
        conn, ids[:1], tmp / "w", tmp / "t", download=lambda u, d: downloads.append(u),
        whisper=lambda p: pytest.fail("no whisper for YouTube"), every_s=30, max_attempts=3,
        captions=transcribe.CaptionFetcher(lambda url: None))
    assert res["failed"] == 1 and downloads == []
    status, reason = conn.execute("select status, fail_reason from items where id=?", (ids[0],)).fetchone()
    assert status == "discovered" and "no transcript" in reason  # retried on a later run


def test_first_captions_tries_sources_in_order():
    calls = []

    def a(url):
        calls.append("a")
        raise transcribe.RateLimited("429")

    def b(url):
        calls.append("b")

    def c(url):
        calls.append("c")
        return "[00:00] text"

    chain = transcribe.first_captions(transcribe.CaptionFetcher(a), transcribe.CaptionFetcher(b),
                                      transcribe.CaptionFetcher(c))
    assert chain("https://www.youtube.com/watch?v=x") == "[00:00] text" and calls == ["a", "b", "c"]
    assert chain("https://www.youtube.com/watch?v=y") == "[00:00] text" and calls[3:] == ["b", "c"]  # a tripped


def test_video_id_from_url():
    assert transcribe.video_id("https://www.youtube.com/watch?v=oVsEddfhdxc&t=3") == "oVsEddfhdxc"
    assert transcribe.video_id("https://youtu.be/oVsEddfhdxc") == "oVsEddfhdxc"


class FakeApi:
    def __init__(self, result):
        self.result = result

    def fetch(self, vid, languages):
        if isinstance(self.result, Exception):
            raise self.result
        return S(snippets=self.result)


from types import SimpleNamespace as S


def test_transcript_api_formats_markers():
    snippets = [S(start=0.0, text="Hi everyone."), S(start=12.0, text="Today evals."), S(start=40.5, text="Next.")]
    out = transcribe.transcript_api_captions("https://www.youtube.com/watch?v=x", every_s=30,
                                             api=FakeApi(snippets))
    assert out.splitlines() == ["[00:00] Hi everyone. Today evals.", "[00:40] Next."]


def test_transcript_api_block_is_rate_limited_and_missing_is_none():
    from youtube_transcript_api._errors import (
        IpBlocked,
        NoTranscriptFound,
        RequestBlocked,
        TranscriptsDisabled,
    )
    url = "https://www.youtube.com/watch?v=x"
    for blocked in (RequestBlocked("x"), IpBlocked("x")):
        with pytest.raises(transcribe.RateLimited):
            transcribe.transcript_api_captions(url, every_s=30, api=FakeApi(blocked))
    assert transcribe.transcript_api_captions(url, every_s=30, api=FakeApi(TranscriptsDisabled("x"))) is None
    assert transcribe.transcript_api_captions(url, every_s=30, api=FakeApi(NoTranscriptFound("x", ["en"], None))) is None


# ---- review 2026-09-29 #1/#2: a blocked or stalled YouTube defers the item, it never burns attempts --------

def test_tripped_breaker_defers_without_charging_an_attempt(yt):
    conn, ids, tmp = yt

    def blocked(url):
        raise transcribe.RateLimited("HTTP Error 429")

    captions = transcribe.first_captions(transcribe.CaptionFetcher(blocked))
    for _night in range(5):  # more nights than max_attempts: still retryable afterwards
        res = transcribe.transcribe_items(conn, ids, tmp / "w", tmp / "t", download=lambda u, d: pytest.fail("no video"),
                                          whisper=lambda p: pytest.fail("no whisper"), every_s=30, max_attempts=3,
                                          captions=captions)
        assert res["deferred"] == 3 and res["failed"] == 0
    rows = conn.execute("select status, attempts, fail_reason from items").fetchall()
    assert all(r[0] == "discovered" and r[1] == 0 and "deferred" in r[2] for r in rows)


def test_genuinely_missing_transcript_still_charges_attempts(yt):
    conn, ids, tmp = yt
    captions = transcribe.first_captions(transcribe.CaptionFetcher(lambda url: None))  # answered: no transcript
    for _night in range(3):
        transcribe.transcribe_items(conn, ids[:1], tmp / "w", tmp / "t", download=lambda u, d: None,
                                    whisper=lambda p: [], every_s=30, max_attempts=3, captions=captions)
    assert conn.execute("select status from items where id=?", (ids[0],)).fetchone()[0] == "failed"


def test_transport_error_in_one_source_defers_to_the_next():
    def boom(url):
        raise ConnectionError("connection reset by peer")

    chain = transcribe.first_captions(transcribe.CaptionFetcher(boom), transcribe.CaptionFetcher(lambda u: "[00:00] ok"))
    assert chain("https://www.youtube.com/watch?v=x") == "[00:00] ok"


def test_every_source_blocked_raises_deferred_not_none():
    def blocked(url):
        raise transcribe.RateLimited("429")

    chain = transcribe.first_captions(transcribe.CaptionFetcher(blocked), transcribe.CaptionFetcher(lambda u: None))
    with pytest.raises(transcribe.TranscriptDeferred):
        chain("https://www.youtube.com/watch?v=x")


def test_transcript_api_timeout_and_transport_errors_are_rate_limited():
    import requests
    for err in (requests.exceptions.ReadTimeout("read timed out"), requests.exceptions.ConnectionError("reset")):
        with pytest.raises(transcribe.RateLimited):
            transcribe.transcript_api_captions("https://www.youtube.com/watch?v=x", every_s=30, api=FakeApi(err))


def test_timeout_session_puts_a_default_timeout_on_every_request(monkeypatch):
    import requests
    seen = []
    monkeypatch.setattr(requests.Session, "request", lambda self, method, url, **kw: seen.append(kw.get("timeout")))
    s = transcribe.timeout_session(7)
    s.get("https://www.youtube.com/watch?v=x")
    s.post("https://www.youtube.com/youtubei/v1/player", json={})
    s.get("https://x", timeout=2)  # an explicit timeout wins
    assert seen == [7, 7, 2]


def test_a_code_bug_in_a_caption_source_is_not_silently_deferred():
    def buggy(url):
        raise TypeError("unexpected keyword argument")

    with pytest.raises(TypeError):
        transcribe.first_captions(transcribe.CaptionFetcher(buggy))("https://www.youtube.com/watch?v=x")
