#!/bin/bash
# Daily Brief watcher: run it from cron on any machine that can reach the API (every 30 min is plenty). The show
# itself has no way to reach a human, so this polls its API and sends an alert through a command you choose.
#
# Conditions (each with its own state file, 12 h repeat suppression and a RESOLVED message when it clears):
#   llm      Claude is logged out, or its token was refused by the last build (/health/llm loggedIn false; no episode can be written until someone runs `claude setup-token`)
#   ready    05:00-05:59 and no segment has a ready episode for the morning (unless today's show is assembled)
#   build    the last two builds failed (a repeating fault, not one transient); a capped one reads as the voice cap
#   feeds    /health/show feeds_failing is non-empty: a source whose every collect failed for feed_alert_hours (builds keep succeeding from the backlog, so only this notices a dead feed)
#   api      the API did not answer two checks in a row
#
# Env:
#   BRIEF_API     base URL of the Daily Brief API (default http://localhost:8765)
#   NOTIFY_CMD    command run with the alert text as its only argument, e.g. a script that posts to Telegram,
#                 ntfy, Slack or email. Exit 0 = delivered. Default: print the alert to stdout.
#   STATE_DIR     where alert state lives (default ~/.local/state/brief-watch)
#   COOLDOWN_S    repeat suppression (default 43200 = 12 h)
#   tests only: BRIEF_FIXTURE_DIR (read show.json / llm.json from a directory instead of curl), NOW_HOUR
set -u

BRIEF_API="${BRIEF_API:-http://localhost:8765}"
STATE_DIR="${STATE_DIR:-$HOME/.local/state/brief-watch}"
COOLDOWN_S="${COOLDOWN_S:-43200}"
NOTIFY_CMD="${NOTIFY_CMD:-}"
mkdir -p "$STATE_DIR"

notify() {  # $1 = text; returns non-zero when delivery failed, so the cooldown is not stamped
  if [ -n "$NOTIFY_CMD" ]; then
    $NOTIFY_CMD "$1"
  else
    echo "$1"
  fi
}

fetch() {  # $1 = show|llm -> JSON on stdout, empty on failure
  if [ -n "${BRIEF_FIXTURE_DIR:-}" ]; then
    cat "$BRIEF_FIXTURE_DIR/$1.json" 2>/dev/null
  else
    curl -fsS --max-time 20 "$BRIEF_API/health/$1" 2>/dev/null
  fi
}

# state machine per condition: $1 name, $2 1=bad 0=good, $3 message when bad
judge() {
  local name="$1" bad="$2" msg="$3" f="$STATE_DIR/$1" now last
  now=$(date +%s)
  if [ "$bad" = "1" ]; then
    last=$(cat "$f" 2>/dev/null || echo 0)
    if [ $((now - last)) -ge "$COOLDOWN_S" ]; then
      # stamp only on delivery, so a dead bot never silences a real incident
      notify "Daily Brief: $msg" && echo "$now" > "$f"
    fi
  elif [ -f "$f" ]; then
    notify "Daily Brief RESOLVED: $name" && rm -f "$f"
  fi
}

show=$(fetch show)
llm=$(fetch llm)

# api: two misses in a row (a counter, separate from the alert stamp)
miss_f="$STATE_DIR/api.misses"
if [ -z "$show" ]; then
  misses=$(( $(cat "$miss_f" 2>/dev/null || echo 0) + 1 )); echo "$misses" > "$miss_f"
  judge api "$([ "$misses" -ge 2 ] && echo 1 || echo 0)" "API $BRIEF_API not answering ($misses checks)"
  exit 0  # nothing else can be judged without the API
fi
echo 0 > "$miss_f"
judge api 0 ""

read_py='import json,sys
d=json.loads(sys.argv[1] or "{}")
k=sys.argv[2]
if k=="llm": print(0 if d.get("loggedIn") is False else 1)
elif k=="ready": print(sum(int(v) for v in (d.get("ready_next") or {}).values()) + sum(1 for v in (d.get("ready_today") or {}).values() if v))
elif k=="outcome": print(((d.get("last_build") or {}).get("outcome") or "")[:40])
elif k=="fails": print(int(d.get("failed_in_a_row", 0)))
elif k=="feeds": print("; ".join("%s since %s: %s" % (n, (v.get("since") or "?")[:16], (v.get("error") or "")[:80]) for n,v in (d.get("feeds_failing") or {}).items()))
elif k=="err": print(((d.get("last_build") or {}).get("error") or "")[:200])'

logged_in=$(python3 -c "$read_py" "$llm" llm 2>/dev/null || echo 1)
judge llm "$([ "$logged_in" = "0" ] && echo 1 || echo 0)" \
  "Claude is logged out (or its token was rejected); no episodes can be written. Fix: run 'claude setup-token' and save the token as CLAUDE_CODE_OAUTH_TOKEN in the secrets file"

hour=$(( 10#${NOW_HOUR:-$(date +%H)} ))
ready=$(python3 -c "$read_py" "$show" ready 2>/dev/null || echo 0)
if [ "$hour" -eq 5 ]; then
  judge ready "$([ "$ready" -eq 0 ] && echo 1 || echo 0)" "no episode is ready for this morning's show"
elif [ "$ready" -gt 0 ]; then
  judge ready 0 ""
fi

fails=$(python3 -c "$read_py" "$show" fails 2>/dev/null || echo 0)
err=$(python3 -c "$read_py" "$show" err 2>/dev/null || echo "")
outcome=$(python3 -c "$read_py" "$show" outcome 2>/dev/null || echo "")
case "$outcome" in
  capped) what="daily voice cap reached (the last $fails builds were capped)" ;;
  "")     what="the last $fails builds failed" ;;
  *)      what="the last $fails builds failed ($outcome)" ;;
esac
judge build "$([ "$fails" -ge 2 ] && echo 1 || echo 0)" "$what: $err"

feeds=$(python3 -c "$read_py" "$show" feeds 2>/dev/null || echo "")
judge feeds "$([ -n "$feeds" ] && echo 1 || echo 0)" "feed(s) failing: $feeds"
