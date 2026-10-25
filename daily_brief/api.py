"""brief-api: today's rundown, feedback, library, brief approval, health.

Serves the phone page at / (self-contained, no build step). It plays nothing: the MP3 and the feed are served
from here and played by whatever the user points at them.
"""

from __future__ import annotations

import datetime as dt
import difflib
import email.utils
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from daily_brief import brief as brief_mod
from daily_brief import db, preflight, queue
from daily_brief.build import llm_env
from daily_brief.config import Config, load_config
from daily_brief.mix import probe_duration

PAGE = Path(__file__).with_name("phone.html")
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ET.register_namespace("itunes", ITUNES)
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
AssembleError = (queue.QueueError, OSError, subprocess.SubprocessError, sqlite3.Error)


class Feedback(BaseModel):
    date: str
    segment: Literal["A", "B"]
    cluster_id: str
    kind: Literal["up", "skip_topic", "deeper"]
    note: str | None = Field(default=None, max_length=500)


class Draft(BaseModel):
    text: str = Field(max_length=50_000)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def create_app(cfg: Config, today: Callable[[], str] | None = None) -> FastAPI:
    var = cfg.paths.var_dir
    today = today or (lambda: dt.datetime.now().astimezone().date().isoformat())
    app = FastAPI(title="Daily Brief", docs_url=None, redoc_url=None)
    app.state.var = str(var)
    app.state.probe = probe_duration  # test seam
    app.state.auth_status = preflight.auth_status  # test seam
    app.state.clock = time.monotonic  # test seam: the /health/llm cache
    app.state.now = lambda: dt.datetime.now().astimezone()  # test seam: "is it past show.assemble_at"
    llm_cache: dict = {}

    def conn() -> sqlite3.Connection:
        return db.connect(var / "brief.db")

    def ready(date: str) -> dict:
        try:
            return json.loads((var / "show" / date / "READY").read_text())
        except (OSError, ValueError):
            return {"segments": {}, "meta": {}}

    @app.get("/health")
    def health():
        c = conn()
        last = _rows(c, "select date, stage, status, finished, error from runs order by id desc limit 8")
        ready_now = set(ready(today())["segments"])
        enabled = list(cfg.pipeline.enabled_segments)
        return {"ok": True, "date": today(), "ready_segments": sorted(ready_now), "enabled_segments": enabled,
                "missing_segments": sorted(set(enabled) - ready_now), "last_runs": last}

    def feeds_failing(c: sqlite3.Connection) -> dict:
        """Feeds whose every collect attempt in the last `collect.feed_alert_hours` failed (and the streak began at
        least that long ago): the builder keeps building from the backlog, so a dead feed never fails a build. Reads
        the per-feed meta of recent build runs, newest first; runs without it (older rows, a crash) don't decide."""
        col = cfg.collect
        now = dt.datetime.now().astimezone()
        start = (now - dt.timedelta(hours=col.feed_alert_lookback_hours)).isoformat(timespec="seconds")
        names = {s.name for s in cfg.sources}
        state: dict[str, dict] = {}  # feed -> {"since", "error"}; "done" once a success ended its streak
        for started, meta in c.execute("select started, meta from runs where stage = 'build' and status != 'running' "
                                       "and started >= ? and meta is not null order by id desc", (start,)):
            try:
                m = json.loads(meta)
                ok, bad = m["feeds_ok"], m["feeds_failed"]
            except (ValueError, KeyError, TypeError):
                continue
            for name in names & (set(ok) | set(bad)):
                st = state.setdefault(name, {})
                if st.get("done"):
                    continue
                if name in ok:
                    st["done"] = True
                    st.setdefault("since", None)  # newest attempt was good: not failing
                else:
                    st["since"] = started
                    st.setdefault("error", bad[name])  # the newest run's error
        limit = (now - dt.timedelta(hours=col.feed_alert_hours)).isoformat(timespec="seconds")
        return {n: {"since": st["since"], "error": st["error"]} for n, st in sorted(state.items())
                if st.get("error") is not None and st["since"] <= limit}

    @app.get("/health/show")
    def health_show():
        """For the watcher and the phone: is the library stocked, did the builder run, what did the voice cost."""
        c = conn()
        ready_next = {seg: c.execute("select count(*) from episodes where segment = ? and status = 'ready' "
                                     "and assigned_date is null", (seg,)).fetchone()[0]
                      for seg in cfg.pipeline.enabled_segments}
        built = [dt.datetime.fromisoformat(r[0]).timestamp()
                 for r in c.execute("select built_at from episodes where status = 'ready'")]
        runs = _rows(c, "select status, error, started, finished, meta from runs where stage = 'build' "
                        "order by id desc")
        last = runs[0] if runs else None
        try:  # the builder's own verdict (ready, capped, failed, ...); a run that died before it has no meta
            outcome = json.loads(last["meta"])["outcome"] if last and last["meta"] else None
        except (ValueError, KeyError, TypeError):
            outcome = None
        in_a_row = 0
        for r in runs:
            if r["status"] == "running":  # in flight (or orphaned by a crash): neither a failure nor a success
                continue
            if r["status"] != "failed":
                break
            in_a_row += 1
        # today's morning already assembled (its READY lists the segments): ready_next is then empty by design
        aired = ready(today()).get("segments") or {}
        ready_today = {seg: seg in aired for seg in cfg.pipeline.enabled_segments}
        return {"ready_next": ready_next, "ready_today": ready_today, "last_built_at": int(max(built, default=0)),
                "last_build": {"status": last["status"], "error": last["error"],
                               "at": last["finished"] or last["started"], "outcome": outcome} if last
                else {"status": None, "error": None, "at": None, "outcome": None},
                "failed_in_a_row": in_a_row, "feeds_failing": feeds_failing(c),
                "spend_today": round(db.spend_today(c, today()), 6), "cap": cfg.voice.daily_cap_usd}

    @app.get("/health/llm")
    def health_llm():
        """Is Claude usable? `claude auth status` (no model call) only checks that a token is SET, not that it is valid,
        so it is combined with the builder's own verdict: the newest build run that reached Claude decides (refused =
        `LLMAuthError` error -> tokenRejected; accepted = meta.llm_ok). Runs that never called Claude (no candidates,
        capped, deferred) don't decide. The subprocess is cached (the watcher polls); the DB part is cheap and read
        every time, so a fixed token clears after the next good build. Only loggedIn / authMethod / tokenRejected are
        returned: the token, error text, email and org never are."""
        now = app.state.clock()
        if llm_cache and now - llm_cache["at"] < cfg.llm.health_cache_seconds:
            safe = llm_cache["status"]
        else:
            try:
                status = app.state.auth_status(str(cfg.llm.command), llm_env(cfg))
            except preflight.PreflightError as e:
                # unreadable is not "logged in": say so, and don't cache it, so the next poll tries again
                return {"loggedIn": False, "error": str(e)}
            # an allowlist, not a pass-through: `claude auth status` also prints the account email and org, and this
            # endpoint has no auth on the LAN
            safe = {"loggedIn": status.get("loggedIn") is True, "authMethod": status.get("authMethod")}
            llm_cache.update(at=now, status=safe)
        rejected = False
        for error, meta in conn().execute("select error, meta from runs where stage = 'build' "
                                          "and status != 'running' order by id desc"):
            if (error or "").startswith("LLMAuthError"):
                rejected = True
                break
            try:
                if json.loads(meta or "{}").get("llm_ok"):
                    break
            except (ValueError, AttributeError):
                continue
        return {"loggedIn": safe["loggedIn"] and not rejected, "authMethod": safe["authMethod"],
                "tokenRejected": rejected}

    @app.get("/today")
    def today_view(date: str | None = None):
        date = date or today()
        c = conn()
        out = {"date": date, "ready": ready(date), "segments": {}, "up_next": []}
        for r in _rows(c, "select segment, json from rundowns where date = ?", (date,)):
            rd = json.loads(r["json"])
            ids = {x["position"]: f"k{x['position']}" for x in rd["rundown"]}
            out["segments"][r["segment"]] = {
                "title": rd.get("segment_title"), "note": rd.get("editor_note"),
                "kept": [{"cluster_id": ids[x["position"]], "role": x["role"], "why": x["why_kept"],
                          "minutes": x["minutes"], "sources": x.get("sources", []), "priority": x["priority"]}
                         for x in rd["rundown"]],
                "cut": [{"reason": x["reason"], "note": x.get("note", "")} for x in rd.get("cut") or []],
                "so_what": (rd.get("so_what") or {}).get("text"),
            }
        fb = _rows(c, "select segment, cluster_id, kind from feedback where date = ?", (date,))
        out["feedback"] = fb
        if date == today() and not out["ready"]["segments"]:
            # not assembled yet (and reading must not assemble: that happens on the first MP3 request or the builder's clock): show what is waiting
            out["up_next"] = [{k: ep[k] for k in ("item_id", "segment", "title", "why", "seconds", "built_at")}
                              for eps in queue.plan(c, cfg).values() for ep in eps]
        return out

    @app.post("/feedback")
    def feedback(f: Feedback):
        c = conn()
        known = c.execute("select 1 from rundown_items where date = ? and segment = ? and cluster_id = ?",
                          (f.date, f.segment, f.cluster_id)).fetchone()
        if not known:
            raise HTTPException(404, f"no item {f.cluster_id} in {f.date} segment {f.segment}")
        c.execute("insert into feedback(date, segment, cluster_id, kind, note) values (?,?,?,?,?)",
                  (f.date, f.segment, f.cluster_id, f.kind, f.note))
        return {"ok": True}

    def _day_dirs() -> dict[str, Path]:
        """date -> folder holding that day's show; the local copy wins over the archive."""
        days: dict[str, Path] = {}
        for base in (cfg.paths.archive_dir, var / "show"):  # local last, so it overrides
            if base.is_dir():
                for d in base.glob("20*-*-*"):
                    if (d / "READY").is_file():
                        days[d.name] = d
        return days

    def _segments_of(folder: Path) -> list[dict]:
        try:
            r = json.loads((folder / "READY").read_text())
        except (OSError, ValueError):
            return []
        out = []
        for seg in cfg.show.segments:
            name = r.get("segments", {}).get(seg)
            p = folder / name if name else None
            if p is None or not p.is_file():
                continue
            m = r.get("meta", {}).get(seg, {})
            out.append({"segment": seg, "title": m.get("title"), "seconds": m.get("seconds"), "path": p})
        return out

    @app.get("/library")
    def library():
        days = _day_dirs()
        out = []
        for date in sorted(days, reverse=True):
            segs = _segments_of(days[date])
            if segs:
                out.append({"date": date, "segments": [{k: v for k, v in x.items() if k != "path"} for x in segs]})
        return {"days": out}

    def show_file(date: str) -> Path | None:
        """The day's joined MP3 if the day was assembled with one (days assembled before v3 have none)."""
        name = ready(date).get("show")
        p = var / "show" / date / name if isinstance(name, str) else None
        return p if p is not None and p.is_file() else None

    def assemble_on_demand(date: str) -> None:
        queue.assemble(conn(), cfg, date)

    @app.get("/shows/{date}.mp3")
    def show_mp3(date: str = PathParam(pattern=DATE_RE.pattern)):
        p = show_file(date)
        if p is None and date == today():  # the first request of the day builds the morning (a no-op if done)
            try:
                assemble_on_demand(date)
            except AssembleError as e:
                raise HTTPException(500, f"could not assemble today's show: {type(e).__name__}: {e}") from e
            p = show_file(date)
        if p is None:
            raise HTTPException(404, f"no show for {date}")
        return FileResponse(p, media_type="audio/mpeg", filename=f"daily-brief-{date}.mp3",
                            content_disposition_type="inline")

    def assembled_dates() -> list[str]:
        """Dates with a joined show, newest first."""
        base = var / "show"
        days = [d.name for d in base.iterdir() if d.is_dir() and DATE_RE.fullmatch(d.name)] if base.is_dir() else []
        return [d for d in sorted(days, reverse=True) if show_file(d)]

    @app.get("/latest.mp3")
    def latest_mp3():
        """Today's show (assembled now if it is past show.assemble_at), else the newest assembled one."""
        if not show_file(today()) and app.state.now().time() >= cfg.show.assemble_at:
            try:
                assemble_on_demand(today())
            except AssembleError:
                pass  # the newest earlier show is still better than an error for a player that just wants audio
        dates = assembled_dates()
        if not dates:
            raise HTTPException(404, "no show has been assembled yet")
        return show_mp3(dates[0])

    @app.get("/feed.xml")
    def feed(request: Request):
        base = cfg.feed.base_url or str(request.base_url).rstrip("/")
        rss = ET.Element("rss", {"version": "2.0"})
        ch = ET.SubElement(rss, "channel")
        for tag, text in (("title", cfg.feed.title), ("link", base), ("description", cfg.feed.description),
                          ("language", "en")):
            ET.SubElement(ch, tag).text = text
        for date in assembled_dates()[:cfg.feed.items]:
            p = show_file(date)
            r = ready(date)
            item = ET.SubElement(ch, "item")
            ET.SubElement(item, "title").text = f"Daily Brief \N{EM DASH} {date}"
            titles = [r["meta"][seg]["title"] for seg in cfg.show.segments
                      if isinstance(r.get("meta"), dict) and (r["meta"].get(seg) or {}).get("title")]
            if titles:
                ET.SubElement(item, "description").text = " \N{MIDDLE DOT} ".join(titles)
            url = f"{base}/shows/{date}.mp3"
            ET.SubElement(item, "enclosure", {"url": url, "length": str(p.stat().st_size), "type": "audio/mpeg"})
            ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = date
            try:
                when = dt.datetime.fromisoformat(r["ready_at"])
            except (KeyError, TypeError, ValueError):
                when = dt.datetime.fromtimestamp(p.stat().st_mtime, dt.UTC)
            ET.SubElement(item, "pubDate").text = email.utils.format_datetime(
                when if when.tzinfo else when.astimezone())
            if isinstance(r.get("show_seconds"), int | float):
                ET.SubElement(item, f"{{{ITUNES}}}duration").text = str(int(r["show_seconds"]))
        body = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
        return Response(body, media_type="application/rss+xml")

    @app.get("/export")
    def export(since: str):
        c = conn()
        return {"feedback": _rows(c, "select f.*, ri.topic, ri.reason as why from feedback f left join rundown_items ri "
                                     "on ri.date = f.date and ri.segment = f.segment and ri.cluster_id = f.cluster_id "
                                     "where f.date >= ? order by f.id", (since,)),
                "ledger": _rows(c, "select * from ledger where last_covered >= ? order by topic_key", (since,))}

    # ---- relevance brief: weekly draft -> the user approves on the phone ----------
    bdir = brief_mod.runtime_dir(cfg)

    def load(path: Path):
        return brief_mod.load_brief(path, cfg.brief.tv_forbidden_terms)

    @app.get("/brief")
    def brief_view():
        cur_path = brief_mod.effective_path(cfg)
        cur_text = cur_path.read_text()
        out = {"current": {"version": load(cur_path).version, "text": cur_text,
                           "source": "approved" if cur_path.parent == bdir else "seed"}, "draft": None}
        d = bdir / "draft.md"
        if d.is_file():
            dtext = d.read_text()
            out["draft"] = {"version": load(d).version, "text": dtext,
                            "diff": "".join(difflib.unified_diff(cur_text.splitlines(True), dtext.splitlines(True),
                                                                 "current", "draft"))}
        return out

    @app.post("/brief/draft")
    def brief_draft(d: Draft):
        bdir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=bdir, suffix=".md", delete=False) as tmp:
            tmp.write(d.text)
        tmp_path = Path(tmp.name)
        try:
            new = load(tmp_path)
            cur = load(brief_mod.effective_path(cfg))
            if new.version <= cur.version:
                raise brief_mod.BriefError(f"draft version {new.version} must be greater than current {cur.version}")
        except brief_mod.BriefError as e:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(422, str(e)) from e
        tmp_path.replace(bdir / "draft.md")
        return {"ok": True, "version": new.version}

    @app.post("/brief/approve")
    def brief_approve():
        d = bdir / "draft.md"
        if not d.is_file():
            raise HTTPException(409, "no draft to approve")
        b = load(d)  # re-validate: the config's forbidden terms may have changed since submission
        hist = bdir / "history"
        hist.mkdir(parents=True, exist_ok=True)
        (hist / f"v{b.version}-{today()}.md").write_text(d.read_text())
        d.replace(bdir / "brief.md")
        return {"ok": True, "version": b.version}

    @app.post("/brief/reject")
    def brief_reject():
        (bdir / "draft.md").unlink(missing_ok=True)
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def page():
        return PAGE.read_text()

    return app


def main(argv: list[str]) -> int:
    import uvicorn

    cfg = load_config(argv[1] if len(argv) > 1 else "config.toml")
    uvicorn.run(create_app(cfg), host=cfg.api.host, port=cfg.api.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
