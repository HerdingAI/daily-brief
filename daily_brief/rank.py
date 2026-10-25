"""Keyword ranker: order episodes by how well their TITLES match the brief.

Matches are against the brief's priority signals; ignore-list terms subtract. Crude but transparent, and free:
it decides only which episode the hourly builder tries first, because the one Claude call per episode makes the
real judgement (and may skip it). pick_backcatalog weights each matched term by its IDF across the catalog so a
rare, specific hit ("evals") beats a pile of generic ones ("product", "building"), normalised by title length.
Only untouched back-catalog items are candidates, so nothing repeats.
"""

from __future__ import annotations

import math
import re
import sqlite3

from daily_brief.brief import Brief

STOPWORDS = {
    "the", "and", "for", "with", "how", "why", "what", "who", "your", "you", "are", "from", "that", "this",
    "into", "about", "like", "when", "our", "its", "his", "her", "their", "should", "can", "not", "but",
    "all", "was", "has", "have", "will", "get", "got", "out", "one", "two", "new", "more", "most", "is",
    "an", "to", "of", "in", "on", "a", "we", "my", "it", "be", "do", "as", "at", "by", "or", "if", "no",
}


def terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9]+", text.lower()) if w not in STOPWORDS and len(w) >= 2}


def _stem(w: str) -> str:
    # tiny plural/verb normaliser so "managers" matches "manager" and "evals" matches "eval"
    for suf in ("ing", "ers", "er", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def _stems(text: str) -> set[str]:
    return {_stem(w) for w in terms(text)}


def brief_stems(b: Brief) -> tuple[set[str], set[str]]:
    """(wanted, avoid) title stems of a brief."""
    # signals only: they are the curated keywords; priority sentences add filler ("now", "own", "worth")
    wanted = set().union(*(_stems(" ".join(p.signals)) for p in b.priorities))
    avoid = set().union(*(_stems(x) for x in b.ignore)) if b.ignore else set()
    return wanted, avoid


def title_score(title: str, wanted: set[str], avoid: set[str]) -> int:
    """Plain overlap count for a handful of new episodes (no catalog to weight against): signals add, ignore terms
    subtract twice."""
    s = _stems(title)
    return len(s & wanted) - 2 * len(s & avoid)


def pick_backcatalog(conn: sqlite3.Connection, b: Brief, n: int, exclude: frozenset[int] = frozenset(),
                     segment: str | None = None) -> list[int]:
    wanted, avoid = brief_stems(b)
    rows = conn.execute(
        "select i.id, i.title, coalesce(i.published_at, '') from items i join sources s on s.id = i.source_id "
        "where i.is_backcatalog = 1 and i.status = 'discovered' and (? is null or s.segment = ?) "
        "and not exists (select 1 from transcripts t where t.item_id = i.id)", (segment, segment)).fetchall()
    rows = [r for r in rows if r[0] not in exclude]
    title_stems = {item_id: _stems(title) for item_id, title, _ in rows}
    df: dict[str, int] = {}
    for st in title_stems.values():
        for w in st:
            df[w] = df.get(w, 0) + 1
    total = max(len(rows), 1)

    def idf(w: str) -> float:
        return math.log((total + 1) / (df.get(w, 0) + 1)) + 1.0

    scored = []
    for item_id, _, published in rows:
        s = title_stems[item_id]
        raw = sum(idf(w) for w in s & wanted) - 2 * sum(idf(w) for w in s & avoid)
        # divide by sqrt(title length) so long, keyword-stuffed titles can't win on volume alone
        scored.append((round(raw / math.sqrt(max(len(s), 1)), 6), published, item_id))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)  # best match first, newer breaks ties
    return [item_id for _, _, item_id in scored[:n]]
