"""Alarm-mode show runner (started by alarm_clock's podcast alarm).

Assembles today's morning from the episode library (queue.assemble), builds the playlist from what is actually
on disk and listed in READY, and hands it to mpv. No music, no window, no fallback audio: the alarm itself falls
back to Spotify when there is nothing to play. Nothing here touches the network or an LLM.
"""

from __future__ import annotations

import datetime as dt
import json
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from daily_brief import db, queue
from daily_brief.config import Config, load_config
from daily_brief.mix import probe_duration

Probe = Callable[[Path], float | None]


class ShowError(RuntimeError):
    """No playable audio at all."""


class Stopped(Exception):
    """The service was asked to stop mid-show (on-demand play, `systemctl stop`, a release)."""


def _raise_stopped(signum, _frame) -> None:
    raise Stopped(signal.Signals(signum).name)


@dataclass(frozen=True)
class Entry:
    kind: str  # always "segment"; the field stays because now_playing.json entries carry it
    label: str
    path: Path
    seconds: float


def _playable(path: Path, probe: Probe, min_s: float) -> float | None:
    if not path.is_file():
        return None
    d = probe(path)
    return d if d is not None and d >= min_s else None


def _ready_segments(var: Path, date: str) -> dict[str, str]:
    marker = var / "show" / date / "READY"
    try:
        return json.loads(marker.read_text()).get("segments", {})
    except (OSError, ValueError):
        return {}  # no READY (or a corrupt one) means nothing from the day is trusted


def plan_show(cfg: Config, var: Path, date: str, probe: Probe) -> list[Entry]:
    min_s = cfg.player.min_segment_seconds
    listed = _ready_segments(var, date)
    plan: list[Entry] = []
    for seg in cfg.show.segments:
        # only files the assembler listed in READY are trusted; a stray file on disk is ignored
        name = listed.get(seg)
        path = var / "show" / date / name if name else None
        d = _playable(path, probe, min_s) if path else None
        if d:
            plan.append(Entry("segment", seg, path, d))
    if not plan:
        raise ShowError(f"no playable segment for {date} in {var}")
    return plan


def mpv_player(playlist: Path, timeout_s: int, volume: int, ipc: Path) -> int:
    ipc.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["mpv", "--no-video", "--really-quiet", f"--volume={volume}", f"--input-ipc-server={ipc}",
           f"--playlist={playlist}"]
    try:
        return subprocess.run(cmd, timeout=timeout_s, check=False).returncode
    except subprocess.TimeoutExpired:
        return 0  # the timeout is only a backstop; the show normally ends with its last segment


def run_show(cfg: Config, var: Path, date: str, now: dt.datetime, probe: Probe = probe_duration,
             player: Callable[..., int] | None = None) -> dict:
    plan = plan_show(cfg, var, date, probe)
    left = int(sum(e.seconds for e in plan)) + 120  # the plan's own length, plus slack for gaps
    playlist = var / "show" / "playlist.m3u"
    playlist.parent.mkdir(parents=True, exist_ok=True)
    playlist.write_text("#EXTM3U\n" + "".join(f"#EXTINF:{int(e.seconds)},{e.kind}: {e.label}\n{e.path}\n"
                                              for e in plan))
    offset, entries = 0.0, []
    for e in plan:
        entries.append(asdict(e) | {"path": str(e.path), "starts_at_s": offset})
        offset += e.seconds
    (var / "now_playing.json").write_text(json.dumps(
        {"date": date, "started": now.isoformat(timespec="seconds"), "entries": entries}, indent=1))
    rc = (player or mpv_player)(playlist, left, cfg.player.volume, cfg.player.ipc_socket)
    # fallback / alarm_source: kept so the run row's meta keeps the keys older readers expect
    return {"played": True, "player_rc": rc, "entries": [f"{e.kind}:{e.label}" for e in plan],
            "fallback": None, "alarm_source": plan[0].label}


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]  # `--alarm` (what brief-show.service passes) is the only mode
    cfg = load_config(args[0] if args else "config.toml")
    now = dt.datetime.now().astimezone()  # local wall clock: the date is the morning's date
    date = now.date().isoformat()
    conn = db.connect(cfg.paths.var_dir / "brief.db")
    run = db.start_run(conn, date, "show")
    signal.signal(signal.SIGTERM, _raise_stopped)
    try:
        status = {"assembled": sorted(queue.assemble(conn, cfg, date).get("segments", {}))}
        status |= run_show(cfg, cfg.paths.var_dir, date, now)
    except Stopped as e:
        # a deliberate stop after the show began is not a failure; without this the row stays 'running' forever
        db.finish_run(conn, run, "ok", meta={"played": True, "stopped": str(e)})
        print(f"show: stopped by {e}")
        return 0
    # top-level boundary: record the failure so the morning check can see it
    except Exception as e:  # noqa: BLE001
        db.finish_run(conn, run, "failed", error=f"{type(e).__name__}: {e}")
        print(f"show FAILED: {e}", file=sys.stderr)
        return 1
    db.finish_run(conn, run, "ok", meta=status)
    print(f"show: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
