"""Entry point — startup banner, self-test, thread bootstrap."""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta

import requests

from tcdd_bot.config import (
    BOT_TOKEN,
    CHECK_INTERVAL_SECONDS,
    STATIONS_FILE,
    SUBS_FILE,
    TCDD_AUTH,
    TCDD_URL,
)
from tcdd_bot.handlers import telegram_loop
from tcdd_bot.stations import load_stations
from tcdd_bot.subscriptions import load_subs, migrate_subscription_dates
from tcdd_bot.tcdd_api import availability_payload, describe_jwt, tcdd_headers
from tcdd_bot.worker import background_loop


def _tcdd_self_test(stations: list[dict]) -> None:
    """Probe TCDD once at startup so auth/header/route issues surface immediately."""
    print("  TCDD probe  : ", end="", flush=True)
    if not TCDD_AUTH:
        print("skipped (no TCDD_AUTH_TOKEN — expect 401 from the worker)")
        return

    # Pick a known-good route so the test isolates auth/headers from bad input.
    # KONYA → İSTANBUL(SÖĞÜTLÜÇEŞME) is a daily YHT corridor; fall back to a
    # user's saved watch if that pair isn't in the catalog for some reason.
    by_name = {s["name"]: s for s in stations}
    konya = by_name.get("KONYA")
    istanbul = by_name.get("İSTANBUL(SÖĞÜTLÜÇEŞME)")
    if konya and istanbul:
        from_id, from_name = konya["id"], konya["name"]
        to_id, to_name = istanbul["id"], istanbul["name"]
    else:
        sample = next(
            (w for u in load_subs().values() for w in u.get("watches") or []),
            None,
        )
        if not sample:
            print("skipped (KONYA/İSTANBUL pair not in catalog and no saved watch)")
            return
        from_id, from_name = sample["from_id"], sample["from_name"]
        to_id, to_name = sample["to_id"], sample["to_name"]

    test_date = (datetime.now() + timedelta(days=7)).strftime("%d-%m-%Y")
    label = f"{from_name}→{to_name} on {test_date}"

    try:
        resp = requests.post(
            TCDD_URL,
            json=availability_payload(from_id, from_name, to_id, to_name, test_date),
            headers=tcdd_headers(),
            timeout=15,
        )
    except Exception as e:
        print(f"network error: {e}")
        return

    # Pull the human-readable `message` out of TCDD's JSON error envelope.
    try:
        body = resp.json()
        body_msg = body.get("message") or body.get("error") or json.dumps(body)[:300]
    except Exception:
        body_msg = resp.text[:300]

    if resp.status_code == 200:
        print(f"OK ({label})")
    elif resp.status_code in (401, 403):
        print(
            f"HTTP {resp.status_code} — TCDD rejected auth/headers.\n"
            f"                Body: {body_msg}"
        )
    else:
        print(
            f"HTTP {resp.status_code} ({label})\n"
            f"                Body: {body_msg}\n"
            f"                (4xx with a 'no service'-style message means auth is fine, "
            f"just a bad route/date)"
        )


def main() -> None:
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    stations = load_stations()
    if not stations:
        print(
            f"ERROR: stations catalog at {STATIONS_FILE} is empty or missing. "
            f"Cannot start.",
            file=sys.stderr,
        )
        sys.exit(1)

    migrate_subscription_dates()

    token_summary = describe_jwt(TCDD_AUTH) if TCDD_AUTH else "NOT set"

    print(f"Starting TCDD multi-user notifier")
    print(f"  bot token   : {BOT_TOKEN[:10]}…")
    print(f"  TCDD auth   : {token_summary}")
    print(f"  poll every  : {CHECK_INTERVAL_SECONDS}s")
    print(f"  subs file   : {SUBS_FILE}")
    print(f"  catalog     : {len(stations)} stations from {STATIONS_FILE.name}")
    _tcdd_self_test(stations)
    print("Press Ctrl+C to stop.\n")

    stop = threading.Event()
    worker = threading.Thread(
        target=background_loop, args=(stop,), name="tcdd-worker", daemon=True
    )
    worker.start()

    try:
        telegram_loop()
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        stop.set()
        worker.join(timeout=5)
