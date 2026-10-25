"""Episode collector: podcast RSS or YouTube channel feed -> items rows.

New episodes (inside new_window_days) and back-catalog episodes (older, only for backfill
sources) are both stored; the builder decides later what to use. One feed = one transaction,
so a fetch or parse failure leaves the DB exactly as it was.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import json
import sqlite3
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"


class CollectError(RuntimeError):
    """A feed could not be fetched or parsed; nothing from it was written."""

    def __init__(self, message: str, errors: dict[str, str] | None = None):
        super().__init__(message)
        self.errors = errors  # feed name -> error, set when every feed failed (build.py records it per feed)


@dataclass(frozen=True)
class SourceSpec:
    segment: str
    kind: str
    name: str
    url: str
    backfill: bool


@dataclass(frozen=True)
class Episode:
    guid: str
    title: str
    url: str | None
    audio_url: str | None
    published_at: dt.datetime | None
    duration_s: int | None


def parse_duration(raw: str | None) -> int | None:
    """itunes:duration is either plain seconds or [HH:]MM:SS."""
    if not raw:
        return None
    try:
        parts = [int(p) for p in raw.strip().split(":")]
    except ValueError:
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs


def _pubdate(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        d = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    # RFC 2822 "-0000" means "UTC, source zone unknown"; Python returns it naive, which then can't be
    # compared with aware datetimes (Megaphone feeds do this). Treat it as UTC.
    return d if d.tzinfo is not None else d.replace(tzinfo=dt.UTC)


def parse_rss(data: bytes) -> list[Episode]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise CollectError(f"cannot parse feed: {e}") from e
    items = root.findall("./channel/item")
    if root.tag != "rss" or root.find("channel") is None:
        raise CollectError("cannot parse feed: not an RSS document")
    out = []
    for it in items:
        enc = it.find("enclosure")
        guid = (it.findtext("guid") or (enc.get("url") if enc is not None else None) or "").strip()
        if not guid:
            continue  # an item with no identity can't be deduplicated; skip it
        out.append(Episode(
            guid=guid,
            title=(it.findtext("title") or "").strip(),
            url=it.findtext("link"),
            audio_url=enc.get("url") if enc is not None else None,
            published_at=_pubdate(it.findtext("pubDate")),
            duration_s=parse_duration(it.findtext(f"{ITUNES}duration")),
        ))
    return out


def parse_youtube_feed(data: bytes) -> list[Episode]:
    """A channel's feeds/videos.xml (Atom): last ~15 uploads with dates; Shorts are dropped."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise CollectError(f"cannot parse feed: {e}") from e
    if root.tag != f"{ATOM}feed":
        raise CollectError("cannot parse feed: not a YouTube Atom feed")
    out = []
    for e in root.findall(f"{ATOM}entry"):
        vid = (e.findtext(f"{YT}videoId") or "").strip()
        link = e.find(f"{ATOM}link")
        href = link.get("href", "") if link is not None else ""
        if not vid or "/shorts/" in href:
            continue
        watch = f"https://www.youtube.com/watch?v={vid}"
        published = e.findtext(f"{ATOM}published")
        out.append(Episode(guid=vid, title=(e.findtext(f"{ATOM}title") or "").strip(), url=watch, audio_url=watch,
                           published_at=dt.datetime.fromisoformat(published) if published else None,
                           duration_s=None))  # the feed has no duration; Shorts are filtered by URL instead
    return out


PARSERS = {"rss": parse_rss, "youtube": parse_youtube_feed}


# ---- YouTube fallback: the channel's /videos tab via yt-dlp ------------------------------------
# YouTube's feeds/videos.xml goes down for every channel at once (404/500 on 2026-09-26 while the
# channels themselves were fine). The /videos tab lists regular uploads only (no Shorts) with a
# duration and an approximate day-level upload date, which is all the new/back-catalog split needs.
def channel_videos_url(feed_url: str) -> str:
    cid = urllib.parse.parse_qs(urllib.parse.urlparse(feed_url).query).get("channel_id", [""])[0]
    if not cid:
        raise CollectError(f"cannot derive a channel from {feed_url!r}: no channel_id parameter")
    return f"https://www.youtube.com/channel/{cid}/videos"


def parse_ytdlp_listing(text: str) -> list[Episode]:
    """One JSON object per line ({id, title, timestamp, duration}); yt-dlp's other chatter is skipped."""
    out = []
    for line in text.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or not d.get("id"):
            continue
        watch = f"https://www.youtube.com/watch?v={d['id']}"
        ts = d.get("timestamp")
        out.append(Episode(guid=d["id"], title=(d.get("title") or "").strip(), url=watch, audio_url=watch,
                           published_at=dt.datetime.fromtimestamp(ts, dt.UTC) if ts else None,
                           duration_s=int(d["duration"]) if d.get("duration") else None))
    return out


def ytdlp_channel_listing(spec: SourceSpec, ytdlp: str, limit: int, timeout_s: int) -> list[Episode]:
    try:
        r = subprocess.run([ytdlp, "--js-runtimes", "node", "--flat-playlist", "--playlist-end", str(limit),
                            "--extractor-args", "youtubetab:approximate_date",
                            "--print", "%(.{id,title,timestamp,duration})j", channel_videos_url(spec.url)],
                           capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as e:  # an OSError, so collect_source keeps the feed error alongside it
        raise OSError(f"yt-dlp listing timed out after {timeout_s}s") from e
    eps = parse_ytdlp_listing(r.stdout)
    if r.returncode != 0 or not eps:
        raise OSError(f"yt-dlp listing exit {r.returncode}, {len(eps)} videos: {r.stderr.strip()[-200:]}")
    return eps


def http_fetch(url: str, timeout_s: int, user_agent: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return r.read()


def sync_sources(conn: sqlite3.Connection, specs: list[SourceSpec]) -> dict[str, int]:
    """Upsert configured sources by URL; returns url -> source id."""
    for s in specs:
        conn.execute(
            "insert into sources(segment, kind, url, name, backfill) values (?,?,?,?,?) "
            "on conflict(url) do update set segment=excluded.segment, kind=excluded.kind, "
            "name=excluded.name, backfill=excluded.backfill, enabled=1",
            (s.segment, s.kind, s.url, s.name, int(s.backfill)))
    return dict(conn.execute("select url, id from sources").fetchall())


def collect_source(conn: sqlite3.Connection, source_id: int, spec: SourceSpec,
                   fetch: Callable[[str], bytes], now: dt.datetime, new_window_days: int,
                   min_episode_s: int, youtube_fallback: Callable[[SourceSpec], list[Episode]] | None = None) -> dict:
    extra: dict = {}
    try:
        try:
            data = fetch(spec.url)
        except OSError as e:
            raise CollectError(f"{spec.name}: fetch failed: {e}") from e
        episodes = PARSERS[spec.kind](data)
    except CollectError as feed_err:
        if spec.kind != "youtube" or youtube_fallback is None:
            raise
        try:
            episodes = youtube_fallback(spec)
        except OSError as e:
            raise CollectError(f"{feed_err}; yt-dlp listing also failed: {e}") from e
        # recorded in the stage result, so a run that only worked through the fallback is visible
        extra = {"via": "ytdlp-listing", "feed_error": str(feed_err)[:200]}
    cutoff = now - dt.timedelta(days=new_window_days)
    stats = {"new": 0, "backcatalog": 0, "skipped_short": 0, "already_known": 0, **extra}
    conn.execute("begin")
    try:
        for ep in episodes:
            if ep.duration_s is not None and ep.duration_s < min_episode_s:
                stats["skipped_short"] += 1
                continue
            is_new = ep.published_at is not None and ep.published_at >= cutoff
            if not is_new and not spec.backfill:
                continue
            cur = conn.execute(
                "insert into items(source_id, guid, title, url, audio_url, published_at, duration_s, "
                "is_backcatalog) values (?,?,?,?,?,?,?,?) on conflict(source_id, guid) do nothing",
                (source_id, ep.guid, ep.title, ep.url, ep.audio_url,
                 ep.published_at.isoformat() if ep.published_at else None, ep.duration_s, int(not is_new)))
            if cur.rowcount == 0:
                stats["already_known"] += 1
            else:
                stats["new" if is_new else "backcatalog"] += 1
        conn.execute("commit")
    except Exception:
        conn.execute("rollback")
        raise
    return stats


def collect_all(conn: sqlite3.Connection, specs: list[SourceSpec], fetch: Callable[[str], bytes],
                now: dt.datetime, new_window_days: int, min_episode_s: int,
                youtube_fallback: Callable[[SourceSpec], list[Episode]] | None = None) -> dict:
    """Collect every configured source (podcast RSS or YouTube channel feed); one bad feed doesn't stop the others."""
    ids = sync_sources(conn, specs)
    out, errors = {}, {}
    for spec in specs:
        try:
            out[spec.name] = collect_source(conn, ids[spec.url], spec, fetch, now, new_window_days, min_episode_s,
                                            youtube_fallback)
        # per-feed boundary: ANY failure in one feed is recorded and the next feed still runs
        except Exception as e:  # noqa: BLE001
            errors[spec.name] = f"{type(e).__name__}: {e}"
    if errors and not out:
        raise CollectError(f"every feed failed: {errors}", errors)
    return {"sources": out, "errors": errors or None}
