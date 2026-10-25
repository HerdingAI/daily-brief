"""brief-api: today's rundown, now playing, feedback, player controls.

Runs next to the player. The phone page at / is self-contained (no build step).
The player is reached through mpv's JSON IPC socket; if no show is running, controls answer 409.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import socket
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from daily_brief import alarm_clock, db, preflight, queue
from daily_brief import brief as brief_mod
from daily_brief.build import llm_env
from daily_brief.config import Config, load_config
from daily_brief.mix import probe_duration

PAGE = Path(__file__).with_name("phone.html")


class PlayerUnavailable(RuntimeError):
    """No mpv IPC socket answering: the show isn't playing."""


class MpvPlayer:
    def __init__(self, socket_path: Path, timeout_s: float = 2.0):
        self.path, self.timeout = socket_path, timeout_s

    def command(self, *args):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(str(self.path))
                s.sendall(json.dumps({"command": list(args)}).encode() + b"\n")
                buf = b""
                while True:  # mpv may interleave event lines; wait for the reply that has "error"
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    for line in buf.split(b"\n"):
                        if line.strip():
                            msg = json.loads(line)
                            if "error" in msg:
                                if msg["error"] != "success":
                                    raise PlayerUnavailable(f"mpv: {msg['error']}")
                                return msg.get("data")
        except (OSError, ValueError) as e:
            raise PlayerUnavailable(str(e)) from e
        raise PlayerUnavailable("mpv closed the connection without replying")


class Feedback(BaseModel):
    date: str
    segment: Literal["A", "B"]
    cluster_id: str
    kind: Literal["up", "skip_topic", "deeper"]
    note: str | None = Field(default=None, max_length=500)


class Draft(BaseModel):
    text: str = Field(max_length=50_000)


class AlarmBody(BaseModel):
    enabled: bool
    time: str
    days: list[int]
    sound: str


class PlayRequest(BaseModel):
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    segment: Literal["A", "B"] | None = None


class Seek(BaseModel):
    to_s: float = Field(ge=0)


class Volume(BaseModel):
    value: int = Field(ge=0, le=100)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def systemd_launcher(cfg: Config):
    """Start on-demand playback as a transient user unit on the same IPC socket as the show."""
    import subprocess

    def launch(playlist: Path) -> None:
        # one player at a time: whatever was playing (scheduled show or an earlier replay) makes way
        subprocess.run(["systemctl", "--user", "stop", "brief-ondemand.service", "brief-show.service"],
                       check=False, capture_output=True)
        r = subprocess.run(["systemd-run", "--user", "--unit=brief-ondemand", "--collect", "/usr/bin/mpv",
                            "--no-video", "--really-quiet", f"--volume={cfg.player.volume}",
                            f"--input-ipc-server={cfg.player.ipc_socket}", f"--playlist={playlist}"],
                           capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise RuntimeError(f"systemd-run failed: {r.stderr.strip()[-200:]}")
    return launch


def create_app(cfg: Config, player=None, today: Callable[[], str] | None = None,
               launcher: Callable[[Path], None] | None = None) -> FastAPI:
    var = cfg.paths.var_dir
    player = player or MpvPlayer(cfg.player.ipc_socket)
    today = today or (lambda: dt.datetime.now().astimezone().date().isoformat())
    app = FastAPI(title="Daily Brief", docs_url=None, redoc_url=None)
    app.state.var = str(var)
    app.state.probe = probe_duration  # test seam
    app.state.auth_status = preflight.auth_status  # test seam
    app.state.clock = time.monotonic  # test seam: the /health/llm cache
    llm_cache: dict = {}
    launch = launcher or systemd_launcher(cfg)

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

    def alarm_view(s: alarm_clock.Setting) -> dict:
        nxt = alarm_clock.next_fire(s, dt.datetime.now().astimezone())
        rows = _rows(conn(), "select date, status, error, meta from runs where stage='alarm' order by id desc limit 1")
        last = None
        if rows:
            r = rows[0]
            last = {"date": r["date"], "status": r["status"], "error": r["error"],
                    "meta": json.loads(r["meta"]) if r["meta"] else None}
        return alarm_clock.as_dict(s) | {"next_fire": nxt.isoformat(timespec="minutes") if nxt else None,
                                         "last_run": last}

    @app.get("/alarm")
    def get_alarm():
        return alarm_view(alarm_clock.load(var))

    @app.put("/alarm")
    def put_alarm(b: AlarmBody):
        try:
            s = alarm_clock.parse(b.model_dump())
        except alarm_clock.SettingError as e:
            raise HTTPException(422, str(e)) from e
        alarm_clock.save(var, s)
        return alarm_view(s)

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
            # not assembled yet (and reading must not assemble: that is for the alarm / Play): show what is waiting
            out["up_next"] = [{k: ep[k] for k in ("item_id", "segment", "title", "why", "seconds", "built_at")}
                              for eps in queue.plan(c, cfg).values() for ep in eps]
        return out

    @app.get("/now-playing")
    def now_playing():
        try:
            plan = json.loads((var / "now_playing.json").read_text())
        except (OSError, ValueError):
            return {"playing": False}
        try:
            pos = player.command("get_property", "playlist-pos")
            t = player.command("get_property", "playback-time")
            duration = player.command("get_property", "duration")
            paused = player.command("get_property", "pause")
        except PlayerUnavailable:
            return {"playing": False, "date": plan.get("date")}
        entries = plan.get("entries", [])
        entry = entries[pos] if isinstance(pos, int) and 0 <= pos < len(entries) else None
        return {"playing": True, "date": plan.get("date"), "entry": entry, "position_s": t,
                "duration_s": duration if duration is not None else (entry or {}).get("seconds"),
                "paused": bool(paused), "index": pos, "count": len(entries)}

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

    def control(*args):
        try:
            player.command(*args)
        except PlayerUnavailable as e:
            raise HTTPException(409, f"player not running: {e}") from e
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

    def _start(date: str, segs: list[dict]) -> dict:
        files = [(x["segment"], x["path"], app.state.probe(x["path"]) or float(x.get("seconds") or 0)) for x in segs]
        playlist = var / "show" / "ondemand.m3u"
        playlist.parent.mkdir(parents=True, exist_ok=True)
        playlist.write_text("#EXTM3U\n" + "".join(f"#EXTINF:{int(s)},segment: {seg}\n{p}\n" for seg, p, s in files))
        offset, entries = 0.0, []
        for seg, p, sec in files:
            entries.append({"kind": "segment", "label": seg, "path": str(p), "seconds": sec, "starts_at_s": offset})
            offset += sec
        (var / "now_playing.json").write_text(json.dumps(
            {"date": date, "on_demand": True, "started": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
             "entries": entries}, indent=1))
        try:
            launch(playlist)
        except RuntimeError as e:
            raise HTTPException(500, str(e)) from e
        return {"ok": True, "action": "started", "date": date, "segments": [seg for seg, _, _ in files]}

    @app.post("/player/play")
    def play(req: PlayRequest | None = None):
        """No body: resume the running show, else start the latest ready day on demand.
        With {date, segment?}: start that recording (from that segment onward), replacing whatever plays."""
        if req is not None and req.date:
            days = _day_dirs()
            if req.date not in days:
                raise HTTPException(404, f"no recording for {req.date}")
            segs = _segments_of(days[req.date])
            if req.segment:
                idx = next((i for i, x in enumerate(segs) if x["segment"] == req.segment), None)
                if idx is None:
                    raise HTTPException(404, f"no segment {req.segment} on {req.date}")
                segs = segs[idx:]
            return _start(req.date, segs)
        try:
            player.command("set_property", "pause", False)
            return {"ok": True, "action": "resumed"}
        except PlayerUnavailable:
            pass
        try:
            queue.assemble(conn(), cfg, today())  # first press of the day builds the morning (a no-op afterwards)
        except Exception as e:
            raise HTTPException(500, f"could not assemble today's show: {type(e).__name__}: {e}") from e
        days = _day_dirs()
        for date in ([today()] if today() in days else []) + sorted((d for d in days if d != today()), reverse=True):
            segs = _segments_of(days[date])
            if segs:
                return _start(date, segs)
        raise HTTPException(409, "nothing to play yet: no segment is ready")

    @app.post("/player/seek")
    def seek(s_: Seek):
        return control("seek", round(s_.to_s, 1) if s_.to_s % 1 else int(s_.to_s), "absolute")

    @app.post("/player/forward")
    def forward():
        return control("seek", cfg.player.seek_step_seconds, "relative")

    @app.post("/player/rewind")
    def rewind():
        return control("seek", -cfg.player.seek_step_seconds, "relative")

    @app.post("/player/stop")
    def stop():
        return control("set_property", "pause", True)

    @app.post("/player/pause")
    def pause():
        return control("set_property", "pause", True)

    @app.post("/player/resume")
    def resume():
        return control("set_property", "pause", False)

    @app.post("/player/skip")
    def skip():
        return control("playlist-next")

    @app.post("/player/back")
    def back():
        return control("seek", -15, "relative")

    @app.post("/player/volume")
    def volume(v: Volume):
        return control("set_property", "volume", v.value)

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
