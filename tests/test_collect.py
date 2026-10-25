import datetime as dt
from pathlib import Path

import pytest

from daily_brief import collect, db

FIXTURE = (Path(__file__).parent / "fixtures" / "example_podcast.rss").read_bytes()
NOW = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.UTC)
SRC = collect.SourceSpec(segment="B", kind="rss", name="Example Podcast", url="https://x/feed.rss", backfill=True)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "b.db")
    yield c
    c.close()


def test_parse_rss_extracts_episodes():
    eps = collect.parse_rss(FIXTURE)
    assert len(eps) == 3
    e = eps[0]
    assert e.guid == "example:post:1003" and e.duration_s == 5813
    assert e.audio_url.startswith("https://") and e.published_at.year == 2026


@pytest.mark.parametrize("raw,secs", [("5813", 5813), ("01:36:53", 5813), ("36:53", 2213), ("", None),
                                      ("abc", None)])
def test_duration_formats(raw, secs):
    assert collect.parse_duration(raw) == secs


def test_sync_sources_upserts(conn):
    sid = collect.sync_sources(conn, [SRC])["https://x/feed.rss"]
    renamed = collect.SourceSpec(**{**SRC.__dict__, "name": "Example Podcast Renamed"})
    assert collect.sync_sources(conn, [renamed])["https://x/feed.rss"] == sid
    assert conn.execute("select name from sources").fetchall() == [("Example Podcast Renamed",)]


def test_collect_inserts_flags_backcatalog_and_skips_short(conn):
    sid = collect.sync_sources(conn, [SRC])[SRC.url]
    stats = collect.collect_source(conn, sid, SRC, fetch=lambda url: FIXTURE, now=NOW,
                                   new_window_days=7, min_episode_s=900)
    rows = conn.execute("select guid, is_backcatalog from items order by published_at desc").fetchall()
    assert rows == [("example:post:1003", 0), ("example:post:1002", 1)]  # trailer skipped
    assert stats == {"new": 1, "backcatalog": 1, "skipped_short": 1, "already_known": 0}


def test_collect_is_idempotent(conn):
    sid = collect.sync_sources(conn, [SRC])[SRC.url]
    kw = {"fetch": lambda url: FIXTURE, "now": NOW, "new_window_days": 7, "min_episode_s": 900}
    collect.collect_source(conn, sid, SRC, **kw)
    stats = collect.collect_source(conn, sid, SRC, **kw)
    assert stats["already_known"] == 2 and stats["new"] == 0
    assert conn.execute("select count(*) from items").fetchone()[0] == 2


def test_source_without_backfill_ignores_old_episodes(conn):
    spec = collect.SourceSpec(**{**SRC.__dict__, "backfill": False})
    sid = collect.sync_sources(conn, [spec])[spec.url]
    collect.collect_source(conn, sid, spec, fetch=lambda url: FIXTURE, now=NOW, new_window_days=7, min_episode_s=900)
    assert conn.execute("select count(*) from items").fetchone()[0] == 1


def test_fetch_failure_raises_and_writes_nothing(conn):
    sid = collect.sync_sources(conn, [SRC])[SRC.url]

    def down(url):
        raise OSError("connection refused")

    with pytest.raises(collect.CollectError, match="connection refused"):
        collect.collect_source(conn, sid, SRC, fetch=down, now=NOW, new_window_days=7, min_episode_s=900)
    assert conn.execute("select count(*) from items").fetchone()[0] == 0


def test_garbage_feed_raises(conn):
    sid = collect.sync_sources(conn, [SRC])[SRC.url]
    with pytest.raises(collect.CollectError, match="parse"):
        collect.collect_source(conn, sid, SRC, fetch=lambda u: b"<html>nope", now=NOW, new_window_days=7,
                               min_episode_s=900)


def test_collect_all_survives_one_bad_feed(conn):
    good = SRC
    bad = collect.SourceSpec(segment="A", kind="rss", name="Broken", url="https://bad/feed", backfill=False)

    def fetch(url):
        if "bad" in url:
            raise OSError("dns")
        return FIXTURE

    res = collect.collect_all(conn, [good, bad], fetch, NOW, 7, 900)
    assert res["sources"]["Example Podcast"]["new"] == 1 and "Broken" in res["errors"]


def test_collect_all_raises_when_every_feed_fails(conn):
    def down(url):
        raise OSError("offline")

    with pytest.raises(collect.CollectError, match="every feed failed"):
        collect.collect_all(conn, [SRC], down, NOW, 7, 900)


def test_minus_zero_timezone_is_treated_as_utc():
    feed = FIXTURE.replace(b"Sun, 20 Sep 2026 12:32:17 GMT", b"Sun, 20 Sep 2026 12:32:17 -0000")
    ep = collect.parse_rss(feed)[0]
    assert ep.published_at.tzinfo is not None and ep.published_at.hour == 12


def test_unexpected_error_in_one_feed_does_not_stop_the_others(conn, monkeypatch):
    bad = collect.SourceSpec(segment="A", kind="rss", name="Weird", url="https://weird/feed", backfill=False)
    real = collect.collect_source

    def flaky(conn_, sid, spec, *a, **k):
        if spec.name == "Weird":
            raise TypeError("something nobody anticipated")
        return real(conn_, sid, spec, *a, **k)

    monkeypatch.setattr(collect, "collect_source", flaky)
    res = collect.collect_all(conn, [bad, SRC], lambda u: FIXTURE, NOW, 7, 900)
    assert res["sources"]["Example Podcast"]["new"] == 1 and "TypeError" in res["errors"]["Weird"]


# ---- YouTube RSS down -> yt-dlp channel listing fallback --------------------------------------
YT = collect.SourceSpec(segment="A", kind="youtube", name="Welch Labs",
                        url="https://www.youtube.com/feeds/videos.xml?channel_id=UConVfxXodg78Tzh5nNu85Ew",
                        backfill=False)
LISTING = "\n".join([  # noqa: FLY002 - one JSON line per row, as yt-dlp prints them
    '{"id": "new1", "title": "Fresh video", "timestamp": 1790380800, "duration": 2119}',   # 2026-09-26
    '{"id": "old1", "title": "Old video", "timestamp": 1782518400, "duration": 2457}',     # 2026-06-27
    '{"id": "nodate", "title": "No date", "timestamp": null, "duration": 1500}',
    'WARNING: not json',
    '{"id": "short1", "title": "Tiny", "timestamp": 1790380800, "duration": 40}',
])


def test_parse_ytdlp_listing():
    eps = collect.parse_ytdlp_listing(LISTING)
    assert [e.guid for e in eps] == ["new1", "old1", "nodate", "short1"]
    assert eps[0].url == eps[0].audio_url == "https://www.youtube.com/watch?v=new1"
    assert eps[0].published_at == dt.datetime(2026, 9, 26, tzinfo=dt.UTC) and eps[0].duration_s == 2119
    assert eps[2].published_at is None


def test_channel_videos_url():
    assert collect.channel_videos_url(YT.url) == "https://www.youtube.com/channel/UConVfxXodg78Tzh5nNu85Ew/videos"
    with pytest.raises(collect.CollectError, match="channel_id"):
        collect.channel_videos_url("https://www.youtube.com/@welchlabs")


def _down(url):
    raise OSError("HTTP Error 404: Not Found")


def test_youtube_feed_failure_uses_listing_fallback(conn):
    sid = collect.sync_sources(conn, [YT])[YT.url]
    seen = []
    stats = collect.collect_source(conn, sid, YT, fetch=_down, now=NOW, new_window_days=7, min_episode_s=900,
                                   youtube_fallback=lambda spec: seen.append(spec.name) or collect.parse_ytdlp_listing(LISTING))
    assert seen == ["Welch Labs"]
    assert stats["new"] == 1 and stats["skipped_short"] == 1
    assert stats["via"] == "ytdlp-listing" and "404" in stats["feed_error"]
    assert [r[0] for r in conn.execute("select guid from items")] == ["new1"]


def test_youtube_garbage_feed_also_falls_back(conn):
    sid = collect.sync_sources(conn, [YT])[YT.url]
    stats = collect.collect_source(conn, sid, YT, fetch=lambda u: b"<html>error page", now=NOW, new_window_days=7,
                                   min_episode_s=900, youtube_fallback=lambda s: collect.parse_ytdlp_listing(LISTING))
    assert stats["via"] == "ytdlp-listing" and stats["new"] == 1


def test_both_youtube_paths_failing_raises_with_both_reasons(conn):
    sid = collect.sync_sources(conn, [YT])[YT.url]

    def listing_down(spec):
        raise OSError("yt-dlp exit 1: Sign in to confirm")

    with pytest.raises(collect.CollectError, match="404.*Sign in"):
        collect.collect_source(conn, sid, YT, fetch=_down, now=NOW, new_window_days=7, min_episode_s=900,
                               youtube_fallback=listing_down)
    assert conn.execute("select count(*) from items").fetchone()[0] == 0


def test_rss_source_never_uses_the_youtube_fallback(conn):
    sid = collect.sync_sources(conn, [SRC])[SRC.url]
    with pytest.raises(collect.CollectError, match="404"):
        collect.collect_source(conn, sid, SRC, fetch=_down, now=NOW, new_window_days=7, min_episode_s=900,
                               youtube_fallback=lambda s: pytest.fail("rss must not fall back"))


def test_working_youtube_feed_does_not_call_fallback(conn):
    feed = (Path(__file__).parent / "fixtures" / "example_youtube_feed.xml").read_bytes()
    sid = collect.sync_sources(conn, [YT])[YT.url]
    stats = collect.collect_source(conn, sid, YT, fetch=lambda u: feed, now=NOW, new_window_days=7, min_episode_s=900,
                                   youtube_fallback=lambda s: pytest.fail("feed worked; no fallback"))
    assert "via" not in stats


def test_collect_all_passes_the_fallback_through(conn):
    res = collect.collect_all(conn, [YT], _down, NOW, 7, 900,
                              youtube_fallback=lambda s: collect.parse_ytdlp_listing(LISTING))
    assert res["sources"]["Welch Labs"]["via"] == "ytdlp-listing" and res["errors"] is None


def _stub_ytdlp(tmp_path, body: str) -> str:
    exe = tmp_path / "yt-dlp"
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(0o755)
    return str(exe)


def test_ytdlp_channel_listing_runs_the_channel_videos_tab(tmp_path):
    args_file = tmp_path / "args"
    exe = _stub_ytdlp(tmp_path, f'echo "$@" > {args_file}\ncat <<"X"\n{LISTING}\nX\n')
    eps = collect.ytdlp_channel_listing(YT, exe, limit=15, timeout_s=30)
    assert [e.guid for e in eps][:2] == ["new1", "old1"]
    args = args_file.read_text()
    assert "--playlist-end 15" in args and "approximate_date" in args
    assert args.strip().endswith("/channel/UConVfxXodg78Tzh5nNu85Ew/videos")


@pytest.mark.parametrize("body", ['echo "ERROR: 429" >&2\nexit 1\n', "exit 0\n"])
def test_ytdlp_channel_listing_failure_or_empty_is_an_oserror(tmp_path, body):
    with pytest.raises(OSError, match="yt-dlp listing"):
        collect.ytdlp_channel_listing(YT, _stub_ytdlp(tmp_path, body), limit=15, timeout_s=30)


def test_ytdlp_listing_timeout_is_an_oserror_so_the_feed_error_is_kept(tmp_path, conn):
    exe = _stub_ytdlp(tmp_path, "sleep 5\n")
    with pytest.raises(OSError, match="timed out"):
        collect.ytdlp_channel_listing(YT, exe, limit=15, timeout_s=1)
    sid = collect.sync_sources(conn, [YT])[YT.url]

    def slow(spec):
        return collect.ytdlp_channel_listing(spec, exe, limit=15, timeout_s=1)

    with pytest.raises(collect.CollectError, match="404.*timed out"):
        collect.collect_source(conn, sid, YT, fetch=_down, now=NOW, new_window_days=7, min_episode_s=900,
                               youtube_fallback=slow)
