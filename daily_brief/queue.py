"""The morning queue: turn the library of ready episodes into one day's show.

The hourly builder (build.py) leaves finished episodes in var/library/ with an `episodes` row. The first caller
that needs a date (the alarm, the phone's Play, `show`) assembles it: per segment, the newest ready episodes that
no morning has taken yet, up to show.max_segment_minutes, are joined into var/show/<date>/<seg>.mp3 and listed in
READY. From there the phone page, TV strip, /library, archive and feedback see the same layout they always did.

Assembly is one SQLite write transaction that also holds the lock while the files are cut, so two callers
(alarm tick + phone) can't both take the same episodes. READY is written last, inside the transaction; anything
that fails before it rolls the assignment back and leaves the episodes unassigned.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path

from daily_brief import ledger, mix
from daily_brief.config import Config

Concat = Callable[[list[Path], Path], None]
Probe = Callable[[Path], float | None]


class QueueError(RuntimeError):
    """The day could not be assembled; nothing was assigned and no READY was written."""


def concat_mp3(paths: list[Path], out: Path) -> None:
    """Join mp3s without re-encoding. Every episode comes from the same mixer, so codec settings match."""
    listing = out.with_name(out.stem + ".concat.txt")
    # the concat demuxer's quoting: a single quote inside a name is written '\''
    listing.write_text("".join("file '{}'\n".format(str(p).replace("'", "'\\''")) for p in paths))
    tmp = out.with_name(out.stem + ".tmp.mp3")
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                            "-i", str(listing), "-c", "copy", str(tmp)],
                           capture_output=True, text=True, timeout=600, check=False)
        if r.returncode != 0:
            raise QueueError(f"ffmpeg concat failed: {r.stderr[-400:]}")
        tmp.replace(out)
    finally:
        listing.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


def _ready_marker(var: Path, date: str) -> Path:
    return var / "show" / date / "READY"


def _read_ready(var: Path, date: str) -> dict | None:
    """The day's READY if it exists (a corrupt one still counts: the day was assembled), else None."""
    try:
        return json.loads(_ready_marker(var, date).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}


def _write_ready(show_dir: Path, data: dict) -> None:
    tmp = show_dir / "READY.tmp"
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(show_dir / "READY")


_EP_FIELDS = ("item_id", "title", "why", "chapters", "so_what", "path", "seconds", "built_at", "creator")
_EP_SELECT = ("select e.item_id, e.title, e.why, e.chapters, e.so_what, e.path, e.seconds, e.built_at, s.name "
              "from episodes e join items i on i.id = e.item_id join sources s on s.id = i.source_id ")


def _playable(conn: sqlite3.Connection, cfg: Config, ep: dict, probe: Probe) -> bool:
    """False for a file ffprobe cannot read or that is shorter than player.min_segment_seconds; such an episode is
    marked 'bad' (not re-probed, never taken). A probe that cannot run at all (no ffprobe, a hang) is not the
    episode's fault: it is skipped this time and left as it is."""
    try:
        seconds = probe(Path(ep["path"]))
    except (OSError, subprocess.SubprocessError):
        return False
    if seconds is not None and seconds >= cfg.player.min_segment_seconds:
        return True
    conn.execute("update episodes set status = 'bad', why = ? where item_id = ?",
                 (f"unplayable: probe says {seconds}s, minimum {cfg.player.min_segment_seconds}s", ep["item_id"]))
    return False


def plan(conn: sqlite3.Connection, cfg: Config, probe: Probe | None = None) -> dict[str, list[dict]]:
    """What the next assembly would take, per segment in play order. Read-only unless `probe` is given, so /today
    can show it without touching ffprobe.

    New episodes before back catalog, newest published first. An episode that would push the segment over
    show.max_segment_minutes is passed over (a shorter, older one may still fit); one whose file is gone is
    never taken. With a probe, a candidate that cannot be played is marked 'bad' and skipped, so one corrupt
    file cannot fail every morning."""
    cap = cfg.show.max_segment_minutes * 60
    out: dict[str, list[dict]] = {}
    for seg in cfg.show.segments:
        total, picked = 0.0, []
        rows = conn.execute(
            _EP_SELECT + "where e.segment = ? and e.status = 'ready' and e.assigned_date is null "
            "order by i.is_backcatalog, i.published_at desc, e.item_id desc", (seg,)).fetchall()
        for row in rows:
            ep = dict(zip(_EP_FIELDS, row, strict=True))
            seconds = ep["seconds"] or 0.0
            if total + seconds > cap or not ep["path"] or not Path(ep["path"]).is_file():
                continue
            if probe is not None and not _playable(conn, cfg, ep, probe):
                continue
            ep["segment"], ep["seconds"] = seg, seconds
            total += seconds
            picked.append(ep)
        if picked:
            out[seg] = picked
    return out


def _title(eps: list[dict]) -> str:
    return eps[0]["title"] if len(eps) == 1 else f"{eps[0]['title']} (+{len(eps) - 1} more)"


def _rundown(eps: list[dict]) -> dict:
    """The old editor's rundown shape, one item per chapter across the episodes in play order, so /today,
    /feedback and /export read it unchanged."""
    items = []
    for ep in eps:
        chapters = json.loads(ep["chapters"] or "[]")
        for ch in chapters:
            items.append({
                "position": len(items) + 1,
                "role": "headline" if not items else "deep_dive",
                "topic": ch.get("topic"), "priority": ch.get("priority"), "why_kept": ch.get("why"),
                # the chapters carry no timings: spread the episode's length evenly over them
                "minutes": round(ep["seconds"] / 60 / len(chapters), 1),
                "sources": [ep["creator"]], "item_id": ep["item_id"], "episode_title": ep["title"]})
    return {"segment_title": _title(eps), "editor_note": " ".join(ep["why"] for ep in eps if ep["why"]),
            "rundown": items,
            "so_what": {"text": " ".join(ep["so_what"] for ep in eps if ep["so_what"]), "projects": []},
            "cut": []}


def _save_rundown(conn: sqlite3.Connection, date: str, segment: str, rundown: dict) -> None:
    # brief_version 0: an assembled morning has no single brief behind it (each episode was written against
    # the brief of its own day)
    conn.execute("insert or replace into rundowns(date, segment, json, brief_version) values (?,?,?,0)",
                 (date, segment, json.dumps(rundown)))
    conn.execute("delete from rundown_items where date = ? and segment = ?", (date, segment))
    for x in rundown["rundown"]:
        conn.execute("insert into rundown_items(date, segment, cluster_id, idea_ids, kept, scores, reason, position, "
                     "topic) values (?,?,?,?,1,?,?,?,?)",
                     (date, segment, f"k{x['position']}", json.dumps([x["item_id"]]), json.dumps({}),
                      x["why_kept"], x["position"], x["topic"]))


CutError = (QueueError, OSError, subprocess.SubprocessError)


def _try_concat(concat: Concat, eps: list[dict], out: Path) -> bool:
    try:
        concat([Path(ep["path"]) for ep in eps], out)
    except CutError:
        return False
    return True


def _cut_segment(concat: Concat, eps: list[dict], out: Path) -> tuple[list[dict] | None, list[dict], bool]:
    """Cut one segment -> (the episodes in the file or None, episodes that fail even alone, ffmpeg proved to work).

    When the join fails, each episode is tried alone: the ones that fail alone are the corrupt files, and the
    rest are joined without them. The caller condemns them only if ffmpeg worked for something, otherwise the
    problem is the machine, not the library."""
    if _try_concat(concat, eps, out):
        return eps, [], True
    scratch = out.with_name(out.stem + ".single.mp3")
    alone = [(ep, _try_concat(concat, [ep], scratch)) for ep in eps]
    scratch.unlink(missing_ok=True)
    good = [ep for ep, ok in alone if ok]
    bad = [ep for ep, ok in alone if not ok]
    if good and bad and _try_concat(concat, good, out):
        return good, bad, True
    return None, bad, bool(good)


def _episodes(conn: sqlite3.Connection, ids: list[int]) -> list[dict]:
    """The given episodes (assigned or not) in the order of `ids`."""
    by_id = {r[0]: dict(zip(_EP_FIELDS, r, strict=True)) for r in conn.execute(
        _EP_SELECT + f"where e.item_id in ({','.join('?' * len(ids))})", ids)}
    return [by_id[i] for i in ids if i in by_id]


def _needs_completion(conn: sqlite3.Connection, date: str, ready: dict) -> bool:
    for seg, meta in (ready.get("meta") or {}).items():
        ids = [int(i) for i in meta.get("items", [])]
        if conn.execute(f"select 1 from episodes where assigned_date is null and item_id in ({','.join('?' * len(ids))})",
                        ids).fetchone() or not conn.execute(
                "select 1 from rundowns where date = ? and segment = ?", (date, seg)).fetchone():
            return True
    return False


def _complete(conn: sqlite3.Connection, date: str, ready: dict) -> dict:
    """READY is written before the commit, so a hard kill between the two leaves READY over episodes the database
    still calls unassigned: the next morning would air them again. Finish that assignment from READY's own item
    lists. Only the episodes READY names are touched, and one that another date already took is left alone."""
    if not _needs_completion(conn, date, ready):
        return ready
    conn.execute("begin immediate")
    try:
        if _needs_completion(conn, date, ready):  # re-checked under the lock: another caller may have fixed it
            for seg, meta in (ready.get("meta") or {}).items():
                ids = [int(i) for i in meta.get("items", [])]
                eps = _episodes(conn, ids)
                if not conn.execute("select 1 from rundowns where date = ? and segment = ?", (date, seg)).fetchone():
                    rundown = _rundown(eps)
                    _save_rundown(conn, date, seg, rundown)
                    ledger.record_covered(conn, date, rundown)
                conn.execute(f"update episodes set assigned_date = ? where assigned_date is null "
                             f"and item_id in ({','.join('?' * len(ids))})", (date, *ids))
        conn.execute("commit")
    except BaseException:
        conn.execute("rollback")
        raise
    return ready


def assemble(conn: sqlite3.Connection, cfg: Config, date: str, concat: Concat = concat_mp3,
             probe: Probe = mix.probe_duration) -> dict:
    """Build `date`'s show from the ready, unassigned episodes. Returns the READY content, or {} when nothing
    is ready (no READY is written, so the alarm falls back to Spotify). Idempotent: a date that has READY is
    returned as it is (after finishing an assignment a crash left half done), and an episode is assigned to
    exactly one date.

    A corrupt library file must not block the morning: candidates that probe badly are marked 'bad' by plan, and
    a segment that still fails to join is rebuilt without the episodes that fail alone. Only when ffmpeg fails
    for everything is the assembly refused."""
    var = cfg.paths.var_dir
    if (done := _read_ready(var, date)) is not None:
        return _complete(conn, date, done)
    show_dir = var / "show" / date
    wrote_ready = False
    conn.execute("begin immediate")  # the write lock also keeps a second assembler out while files are cut
    try:
        if (done := _read_ready(var, date)) is not None:  # another caller finished while we waited for the lock
            conn.execute("rollback")
            return _complete(conn, date, done)
        chosen = plan(conn, cfg, probe)
        if not chosen:
            conn.execute("commit")  # not a rollback: plan may have marked unplayable episodes
            return {}
        show_dir.mkdir(parents=True, exist_ok=True)
        ready_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        ready: dict = {"segments": {}, "meta": {}}
        corrupt: list[dict] = []
        ffmpeg_works = False
        for seg, planned in chosen.items():
            out = show_dir / f"{seg}.mp3"
            eps, bad, works = _cut_segment(concat, planned, out)
            corrupt += bad
            ffmpeg_works = ffmpeg_works or works
            if eps is None:
                continue
            rundown = _rundown(eps)
            _save_rundown(conn, date, seg, rundown)
            ledger.record_covered(conn, date, rundown)
            ids = [ep["item_id"] for ep in eps]
            conn.execute(f"update episodes set assigned_date = ? where item_id in ({','.join('?' * len(ids))})",
                         (date, *ids))
            ready["segments"][seg] = out.name
            ready["meta"][seg] = {"title": _title(eps), "seconds": round(probe(out) or sum(e["seconds"] for e in eps), 1),
                                  "items": ids, "ready_at": ready_at}
        if not ready["segments"] and not ffmpeg_works:
            raise QueueError("ffmpeg failed for every segment; no episode was condemned")
        for ep in corrupt if ffmpeg_works else []:
            conn.execute("update episodes set status = 'bad', why = 'unplayable: ffmpeg cannot join it' "
                         "where item_id = ?", (ep["item_id"],))
        if not ready["segments"]:  # every segment failed in a way that names no culprit: keep the bad marks only
            conn.execute("commit")
            return {}
        _write_ready(show_dir, ready)  # last: until this line nothing above is visible to a player
        wrote_ready = True
        conn.execute("commit")
    except BaseException:
        conn.execute("rollback")
        if wrote_ready:  # the commit itself failed: a READY over unassigned episodes would air them twice
            _ready_marker(var, date).unlink(missing_ok=True)
        raise
    return ready
