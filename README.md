# TCDD Availability Bot

A multi-user Telegram bot that watches the [TCDD](https://ebilet.tcddtasimacilik.gov.tr/)
ticket API for EKONOMİ-class seats and **auto-holds a seat** the moment one
opens on a train you care about. You then have ~10 minutes to complete
payment on the TCDD website.

The bot is written in pure Python (only `requests` as a third-party dep) and
runs as a single long-lived process — no database, no message broker, no
framework. Per-user state lives in [subscriptions.json](subscriptions.json),
station IDs live in [stations.json](stations.json).

## How it works

1. Each Telegram user adds one or more **watches** through the `/add` flow
   (route + date + train numbers).
2. A background thread polls the TCDD `train-availability` endpoint every
   60 seconds. Watches sharing the same `(from, to, date)` triplet are
   coalesced into a single API call.
3. When an EKONOMİ seat appears on a watched train, the bot:
   - calls `load-by-train-id` to fetch the wagon/seat layout,
   - picks the first free seat in the EKONOMİ cabin class,
   - calls `select-seat` to lock it into the basket,
   - DMs the user the seat number and the booking URL.
4. The user can tap **Release seat** to instantly free the hold (useful if
   someone else in the family already booked), **Keep watching** if the
   train sells out before they pay, or **Stop** to drop that train.

If the auto-hold fails (e.g. TCDD rejects the select-seat call), the user
still gets a plain availability alert and can book manually.

## Project layout

```
tcdd.py                  Launcher — equivalent to `python -m tcdd_bot`
tcdd_bot/
  main.py                Startup banner, TCDD self-test, thread bootstrap
  config.py              Env vars, URLs, intervals, file paths
  handlers.py            Telegram command dispatch + interactive /add flow
  worker.py              60s polling loop, alert + auto-hold orchestration
  tcdd_api.py            TCDD HTTP client, JWT helpers, response parsing
  seat_hold.py           load-by-train-id  →  pick free seat  →  select-seat
  seat_release.py        Release a previously held seat
  telegram_api.py        Thin wrapper around the Telegram Bot API
  subscriptions.py       Per-user watch persistence (subscriptions.json)
  stations.py            Station catalog + fuzzy name matching
  storage.py             Atomic JSON read/write
stations.json            Official TCDD station catalog (id → name)
subscriptions.json       Runtime state — created on first /add
```

## Telegram commands

| Command       | What it does                                            |
|---------------|---------------------------------------------------------|
| `/add`        | Interactive flow to add a watch (4 steps)               |
| `/list`       | Show your active watches with their IDs                 |
| `/remove <N>` | Remove watch #N (IDs come from `/list`)                 |
| `/pause`      | Pause all alerts for your account                       |
| `/resume`     | Resume alerts                                           |
| `/cancel`     | Abort an in-progress `/add`                             |
| `/help`       | Show the command list                                   |

Inline buttons attached to bot messages:
- **Release seat** — frees a held seat back to inventory.
- **Keep watching** / **Stop** — appear when a previously-alerted train sells out.

## Requirements

- Python 3.10+
- The `requests` library (`pip install requests`)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A TCDD bearer token captured from a logged-in browser session

### Capturing the TCDD bearer token

1. Open <https://ebilet.tcddtasimacilik.gov.tr/> and log in.
2. Run any search so the site fires a `train-availability` request.
3. DevTools → Network → click the `train-availability` request.
4. Copy the full value of the `Authorization` request header
   (including the `Bearer ` prefix).

TCDD JWTs are short-lived. If the bot starts logging `401`/`403`, capture a
fresh token and restart the process.

## Run locally

```bash
export TELEGRAM_BOT_TOKEN="<your bot token>"
export TCDD_AUTH_TOKEN="Bearer <token captured from browser>"

pip install requests
python tcdd.py
```

At startup the bot prints a banner with the JWT TTL and runs a one-shot
self-test against a known KONYA → İSTANBUL route so auth/header problems
surface immediately instead of being hidden inside the poll loop.

## Run on a DigitalOcean Droplet

The bot is a single Python process — keep it alive inside `tmux` so it
survives SSH disconnects.

```bash
ssh root@207.154.209.191

cd TCDD-Bot

# new session, or reattach if it already exists
tmux new -s bot_session     # or: tmux attach -t bot_session

python3 tcdd.py
```

Detach without stopping the bot: **Ctrl+B**, then **D**.

Reattach later with `tmux attach -t bot_session`.

Remember to `export TELEGRAM_BOT_TOKEN=…` and `export TCDD_AUTH_TOKEN=…`
inside the tmux session before launching, or set them in `~/.bashrc` /
a sourced env file.

## Configuration knobs

All defaults live in [tcdd_bot/config.py](tcdd_bot/config.py):

| Setting                     | Default                | Notes                                     |
|-----------------------------|------------------------|-------------------------------------------|
| `CHECK_INTERVAL_SECONDS`    | `60`                   | Poll interval for the background worker   |
| `PER_API_CALL_DELAY`        | `1.5`                  | Spacing between grouped API calls         |
| `TELEGRAM_LONG_POLL_TIMEOUT`| `25`                   | Telegram `getUpdates` long-poll timeout   |
| `TARGET_CABIN_CLASS`        | `"EKONOMİ"`            | Cabin class watched and held              |
| `BOOKING_GENDER`            | `"M"`                  | Sent with the `select-seat` payload       |
| `DISPLAY_TZ`                | `GMT+03:00`            | Türkiye time (TCDD returns UTC epoch ms)  |
| `TCDD_HTTP_TIMEOUT`         | `30`                   | Per-attempt timeout for TCDD requests     |
| `TCDD_HTTP_RETRIES`         | `1`                    | Extra attempts on read timeout            |

## Known limitations

- **Single TCDD account.** Every auto-hold is placed under the account that
  owns the `TCDD_AUTH_TOKEN`. Two users cannot have simultaneous holds for
  the same train through this bot.
- **EKONOMİ only.** Business/disabled cabins are intentionally ignored.
- **No HTTPS / no auth on the bot itself.** Anyone who finds the Telegram
  bot username can interact with it; abuse protection is delegated to
  Telegram (block / mute).
- **Stations catalog is static.** Add new stations by editing
  [stations.json](stations.json) (objects with `id` + `name`).

## Potential improvement and ideas

TCDD typically restricts seat holds to 10 minutes. However, we can potentially bypass this by implementing a "heartbeat" select-seat request. By re-issuing a select-seat request every 10 minutes, the bot can effectively renew the hold indefinitely until the user manually releases it or completes the purchase.

The bot will monitor TCDD’s responses to ensure the hold remains active. If the system blocks the request or the seat is lost to another user, the bot will immediately notify the user, revert to "watch mode," and attempt to secure the next available seat.

## License

Personal project — no license file included.
