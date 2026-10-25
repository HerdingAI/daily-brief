#!/bin/bash
# Fixture test for brief-watch.sh: drives every condition through ALERT -> SUPPRESSED -> RESOLVED with
# a stub NOTIFY_CMD (no network). Run: bash ops/watcher/test-brief-watch.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/fx"
# a stub notifier with the real contract: argument = text, exit 0 = delivered (unless forced to fail)
cat > "$tmp/notify.sh" <<'EOF'
#!/bin/bash
echo "$1" >> "$NOTIFY_LOG"; [ "${FORCE_FAIL:-0}" = "1" ] && exit 1; exit 0
EOF
chmod +x "$tmp/notify.sh"
export NOTIFY_CMD="$tmp/notify.sh" STATE_DIR="$tmp/state" BRIEF_FIXTURE_DIR="$tmp/fx" NOTIFY_LOG="$tmp/log"
pass=0 fail=0
run() { : > "$NOTIFY_LOG"; bash "$here/brief-watch.sh"; }
expect() {  # $1 description, $2 grep pattern expected in the log ("" = log must be empty)
  if [ -z "$2" ]; then
    if [ ! -s "$NOTIFY_LOG" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1 (log: $(cat "$NOTIFY_LOG"))"; fi
  elif grep -q -- "$2" "$NOTIFY_LOG"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1 (log: $(cat "$NOTIFY_LOG"))"; fi
}
good_show='{"ready_next":{"A":1,"B":1},"ready_today":{"A":false,"B":false},"failed_in_a_row":0,"last_build":{"status":"ok","error":null,"outcome":"ready"}}'
good_llm='{"loggedIn":true}'

echo "$good_show" > "$tmp/fx/show.json"; echo "$good_llm" > "$tmp/fx/llm.json"
NOW_HOUR=05 run; expect "all good is silent" ""

echo '{"loggedIn":false}' > "$tmp/fx/llm.json"
NOW_HOUR=12 run; expect "logged out alerts" "logged out"
NOW_HOUR=12 run; expect "logged out suppressed on repeat" ""
echo "$good_llm" > "$tmp/fx/llm.json"
NOW_HOUR=12 run; expect "login back resolves" "RESOLVED: llm"

echo '{"ready_next":{"A":0,"B":0},"failed_in_a_row":0}' > "$tmp/fx/show.json"
NOW_HOUR=14 run; expect "empty queue mid-day is not an alert" ""
NOW_HOUR=05 run; expect "empty queue at 05h alerts" "no episode is ready"
echo "$good_show" > "$tmp/fx/show.json"
NOW_HOUR=09 run; expect "queue refilled resolves" "RESOLVED: ready"

# the morning was assembled early (alarm/Play before 05:00): ready_next is 0 by design, and that is fine
echo '{"ready_next":{"A":0,"B":0},"ready_today":{"A":true,"B":true},"failed_in_a_row":0}' > "$tmp/fx/show.json"
NOW_HOUR=05 run; expect "assembled today is not 'nothing ready'" ""
echo '{"ready_next":{"A":0,"B":0},"ready_today":{"A":false,"B":true},"failed_in_a_row":0}' > "$tmp/fx/show.json"
NOW_HOUR=05 run; expect "one segment assembled today is fine too" ""
echo '{"ready_next":{"A":0,"B":0},"ready_today":{"A":false,"B":false},"failed_in_a_row":0}' > "$tmp/fx/show.json"
NOW_HOUR=05 run; expect "nothing next and nothing today still alerts" "no episode is ready"
echo "$good_show" > "$tmp/fx/show.json"
NOW_HOUR=09 run; expect "stocked again resolves" "RESOLVED: ready"

echo '{"ready_next":{"A":1},"failed_in_a_row":2,"last_build":{"status":"failed","error":"HeyGen TTS HTTP 503","outcome":"failed"}}' > "$tmp/fx/show.json"
NOW_HOUR=12 run; expect "two failed builds alert with the error" "HTTP 503"
expect "the alert names the outcome" "builds failed (failed)"
echo "$good_show" > "$tmp/fx/show.json"
NOW_HOUR=12 run; expect "a good build resolves" "RESOLVED: build"

echo '{"ready_next":{"A":1},"failed_in_a_row":3,"last_build":{"status":"failed","error":"capped: $0.140 spent","outcome":"capped"}}' > "$tmp/fx/show.json"
NOW_HOUR=12 run; expect "capped builds read as the voice cap" "daily voice cap reached"
echo "$good_show" > "$tmp/fx/show.json"
NOW_HOUR=12 run; expect "cap cleared resolves" "RESOLVED: build"

echo '{"ready_next":{"A":1},"failed_in_a_row":2,"last_build":{"status":"failed","error":"LLMAuthError: out"}}' > "$tmp/fx/show.json"
NOW_HOUR=12 run; expect "a run that died before any outcome still alerts" "builds failed: LLMAuthError"
echo "$good_show" > "$tmp/fx/show.json"
NOW_HOUR=12 run

echo '{"ready_next":{"A":1},"failed_in_a_row":0,"feeds_failing":{"Welch Labs":{"since":"2026-10-24T03:00:00-05:00","error":"HTTPError: 404"}}}' > "$tmp/fx/show.json"
NOW_HOUR=12 run; expect "a dead feed alerts naming feed and error" "Welch Labs since 2026-10-24T03:00: HTTPError: 404"
NOW_HOUR=12 run; expect "a dead feed is suppressed on repeat" ""
echo "$good_show" > "$tmp/fx/show.json"
NOW_HOUR=12 run; expect "feed recovered resolves" "RESOLVED: feeds"

rm -f "$tmp/fx/show.json"
NOW_HOUR=12 run; expect "one API miss is silent" ""
NOW_HOUR=12 run; expect "two API misses alert" "not answering"
echo "$good_show" > "$tmp/fx/show.json"
NOW_HOUR=12 run; expect "API back resolves" "RESOLVED: api"

# a failed delivery must not stamp the cooldown: the next run tries again
echo '{"loggedIn":false}' > "$tmp/fx/llm.json"
FORCE_FAIL=1 NOW_HOUR=12 run; expect "failed delivery attempted" "logged out"
NOW_HOUR=12 run; expect "retried after failed delivery" "logged out"

echo "brief-watch: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
