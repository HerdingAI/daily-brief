"""Run once a minute by brief-alarm.timer: fire the configured alarm if it is due."""

from __future__ import annotations

import datetime as dt
import sys

from daily_brief import alarm_clock, db
from daily_brief.config import load_config


def main(argv: list[str]) -> int:
    cfg = load_config(argv[1] if len(argv) > 1 else "config.toml")
    conn = db.connect(cfg.paths.var_dir / "brief.db")
    result = alarm_clock.tick(cfg, dt.datetime.now().astimezone(), conn)
    if result["fired"]:
        print(f"alarm: {result}")
    return 1 if result["fired"] and result["used"] is None else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
