# Watcher

`brief-watch.sh` polls the Daily Brief API (`/health/show`, `/health/llm`) and alerts through a command of your
choice. Run it from cron on any machine that can reach the API:

    */30 * * * * BRIEF_API=http://localhost:8765 NOTIFY_CMD=/path/to/send-alert.sh /path/to/ops/watcher/brief-watch.sh

`NOTIFY_CMD` receives the alert text as its single argument (post it to Telegram, ntfy, Slack, email...). With no
`NOTIFY_CMD` the alert is printed. Each alert repeats at most every 12 hours (`COOLDOWN_S`) and sends
`Daily Brief RESOLVED: <name>` when it clears.

| Alert | Meaning | Fix |
|---|---|---|
| `llm` | `/health/llm` says `loggedIn: false`: Claude is logged out or its token was rejected, no episode can be written | `claude setup-token`, save the token in the secrets file |
| `ready` | between 05:00 and 05:59 no segment has a ready episode for the morning | look at the builder's last run; the morning will have no show |
| `build` | `/health/show` `failed_in_a_row` is 2 or more; the message carries `last_build.error` | read the error: TTS provider, cap reached, bad scripts |
| `feeds` | `/health/show` `feeds_failing` is non-empty: a source had no successful collect for `[collect] feed_alert_hours` (24); the message names the feed, since when, and the error | the feed URL changed or the channel is gone: fix or remove it in `config.toml` |
| `api` | the API did not answer two checks in a row | the machine or `brief-api.service` is down |

Test it with `bash ops/watcher/test-brief-watch.sh` (fixture JSON, no network; also run by pytest).
