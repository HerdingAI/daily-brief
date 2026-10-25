"""The configurable alarm: its stored setting, the pure rules for when it is due, and the tick that acts on it.

The setting lives in var/alarm.json (written atomically). Missing or corrupt means the defaults, which
are OFF, so a damaged file can never wake anyone at a surprising time.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from daily_brief import db, queue, show
from daily_brief.config import Config
from daily_brief.mix import probe_duration

SOUNDS = ("podcast", "spotify")
_KEYS = {"enabled", "time", "days", "sound"}


class SettingError(ValueError):
    """The alarm setting is not valid."""


@dataclass(frozen=True)
class Setting:
    enabled: bool = False
    time: str = "06:00"
    days: tuple[int, ...] = field(default=(0, 1, 2, 3, 4, 5, 6))  # Monday=0 .. Sunday=6
    sound: str = "podcast"


def _hhmm(value) -> dt.time:
    if not isinstance(value, str):
        raise SettingError("time must be a string HH:MM")
    hh, sep, mm = value.partition(":")
    try:
        if not (sep and len(hh) == 2 and len(mm) == 2):
            raise ValueError
        return dt.time(int(hh), int(mm))
    except ValueError:
        raise SettingError(f"time must be HH:MM (24h), got {value!r}") from None


def parse(raw: dict) -> Setting:
    if not isinstance(raw, dict) or set(raw) != _KEYS:
        raise SettingError(f"alarm must have exactly the keys {sorted(_KEYS)}")
    if not isinstance(raw["enabled"], bool):
        raise SettingError("enabled must be true or false")
    _hhmm(raw["time"])
    days = raw["days"]
    # `type(d) is int` on purpose: True/False are ints in Python but are not weekdays
    if (not isinstance(days, list) or not days or len(set(days)) != len(days)
            or not all(type(d) is int and 0 <= d <= 6 for d in days)):
        raise SettingError("days must be a non-empty list of distinct weekday numbers 0 (Mon) .. 6 (Sun)")
    if raw["sound"] not in SOUNDS:
        raise SettingError(f"sound must be one of {SOUNDS}")
    return Setting(raw["enabled"], raw["time"], tuple(sorted(days)), raw["sound"])


def as_dict(s: Setting) -> dict:
    return {"enabled": s.enabled, "time": s.time, "days": list(s.days), "sound": s.sound}


def load(var: Path) -> Setting:
    try:
        return parse(json.loads((Path(var) / "alarm.json").read_text()))
    except (OSError, ValueError):  # SettingError is a ValueError: corrupt or invalid -> defaults (off)
        return Setting()


def save(var: Path, s: Setting) -> None:
    var = Path(var)
    var.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=var, prefix="alarm.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(as_dict(s), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, var / "alarm.json")
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _at(day: dt.datetime, t: dt.time) -> dt.datetime:
    return day.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


def is_due(s: Setting, now: dt.datetime, window_minutes: int, already_fired: bool) -> bool:
    if not s.enabled or already_fired or now.weekday() not in s.days:
        return False
    late = (now - _at(now, _hhmm(s.time))).total_seconds()
    return 0 <= late < window_minutes * 60


def next_fire(s: Setting, now: dt.datetime) -> dt.datetime | None:
    if not s.enabled:
        return None
    t = _hhmm(s.time)
    for ahead in range(8):
        cand = _at(now + dt.timedelta(days=ahead), t)
        if cand >= now and cand.weekday() in s.days:
            return cand
    return None


def marker_path(var: Path, date: str) -> Path:
    return Path(var) / "alarm" / f"fired-{date}"


class AlarmError(RuntimeError):
    """A sound could not be started."""


def default_start_podcast() -> None:
    r = subprocess.run(["systemctl", "--user", "start", "--no-block", "brief-show.service"],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise AlarmError(f"systemctl start brief-show failed: {r.stderr.strip()[-200:]}")


def default_start_spotify(cfg: Config) -> None:
    ac = cfg.alarm_clock
    body = json.dumps({"action": "alarm-play", "fadeSeconds": ac.spotify_fade_seconds,
                       "fromPercent": ac.spotify_from_percent}).encode()
    req = urllib.request.Request(ac.bridge_url.rstrip("/") + "/control", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=ac.bridge_timeout_seconds) as resp:
            if resp.status != 200:
                raise AlarmError(f"Spotify bridge answered {resp.status}")
    except OSError as e:  # URLError, HTTPError, timeouts and refused connections
        raise AlarmError(f"Spotify bridge: {e}") from e


def default_podcast_ok(cfg: Config, conn, date: str, now: dt.datetime) -> bool:
    """Assemble today's morning from the library (a no-op once READY exists), then check it can play.

    An assembly failure becomes an AlarmError, never an escaped exception: the marker is already written, so a
    crash here would mean no alarm at all instead of the Spotify fallback."""
    try:
        queue.assemble(conn, cfg, date)
    except Exception as e:
        raise AlarmError(f"could not assemble the morning: {type(e).__name__}: {e}") from e
    try:
        return bool(show.plan_show(cfg, cfg.paths.var_dir, date, probe_duration))
    except show.ShowError:
        return False


def tick(cfg: Config, now: dt.datetime, conn, *, start_podcast=default_start_podcast,
         start_spotify=default_start_spotify, podcast_ok=default_podcast_ok) -> dict:
    """Fire the alarm if it is due; the chosen sound first, the other one if that fails."""
    var = cfg.paths.var_dir
    setting = load(var)
    date = now.date().isoformat()
    marker = marker_path(var, date)
    if not is_due(setting, now, cfg.alarm_clock.window_minutes, marker.exists()):
        return {"fired": False}
    # Marker BEFORE the sound: a crash, or a second tick in the same minute, must never fire twice.
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(now.isoformat(timespec="seconds"))
    run = db.start_run(conn, date, "alarm")
    other = "spotify" if setting.sound == "podcast" else "podcast"
    reasons: dict[str, str] = {}
    for sound in (setting.sound, other):
        try:
            if sound == "podcast":
                if not podcast_ok(cfg, conn, date, now):
                    raise AlarmError("no playable segment for the podcast alarm")
                start_podcast()
            else:
                start_spotify(cfg)
        except AlarmError as e:
            reasons[sound] = str(e)
            continue
        fallback = reasons.get(setting.sound) if sound != setting.sound else None
        meta = {"sound": setting.sound, "used": sound, "fallback_reason": fallback}
        db.finish_run(conn, run, "ok", meta=meta)
        return {"fired": True} | meta
    meta = {"sound": setting.sound, "used": None, "fallback_reason": None, "errors": reasons}
    db.finish_run(conn, run, "failed", error="; ".join(f"{k}: {v}" for k, v in reasons.items()), meta=meta)
    return {"fired": True} | meta
