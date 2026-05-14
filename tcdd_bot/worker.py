"""Background polling loop — TCDD availability check + auto-hold + alert."""
from __future__ import annotations

import sys
import threading
import time
import traceback
from datetime import datetime

from tcdd_bot.config import CHECK_INTERVAL_SECONDS, PER_API_CALL_DELAY
from tcdd_bot.seat_hold import try_hold_seat
from tcdd_bot.subscriptions import (
    clear_notified,
    load_subs,
    mark_awaiting_confirm,
    mark_notified,
)
from tcdd_bot.tcdd_api import collect_train_availability, query_availability
from tcdd_bot.telegram_api import tg_send


def background_loop(stop_event: threading.Event) -> None:
    print("[worker] started")
    while not stop_event.is_set():
        try:
            run_one_cycle()
        except Exception:
            print("[worker] cycle error:", file=sys.stderr)
            traceback.print_exc()
        stop_event.wait(CHECK_INTERVAL_SECONDS)
    print("[worker] stopped")


def run_one_cycle() -> None:
    subs = load_subs()
    if not subs:
        return

    # Group watches by (from, to, date) so multiple users share one API call.
    grouped: dict[tuple, list[tuple[int, dict]]] = {}
    for chat_key, user in subs.items():
        if user.get("paused"):
            continue
        for w in user.get("watches") or []:
            key = (w["from_id"], w["to_id"], w["date"])
            grouped.setdefault(key, []).append((int(chat_key), w))

    first = True
    for key, entries in grouped.items():
        if not first:
            time.sleep(PER_API_CALL_DELAY)
        first = False

        representative = entries[0][1]
        data = query_availability(representative)
        ts = datetime.now().strftime("%H:%M:%S")
        if data is None:
            print(
                f"[{ts}] {representative['from_name']}→{representative['to_name']} "
                f"{representative['date'][:10]}: api error"
            )
            continue

        train_avail = collect_train_availability(
            data, representative["from_id"], representative["to_id"]
        )
        for chat_id, watch in entries:
            handle_user_watch(chat_id, watch, train_avail)


def _keep_stop_keyboard(watch_id: int, train_number: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Keep watching", "callback_data": f"k:{watch_id}:{train_number}"},
            {"text": "🛑 Stop", "callback_data": f"s:{watch_id}:{train_number}"},
        ]],
    }


def handle_user_watch(chat_id: int, watch: dict, train_avail: dict[str, dict]) -> None:
    explicit = {str(n).strip() for n in (watch.get("train_numbers") or []) if str(n).strip()}
    notified = set(watch.get("notified_trains") or [])
    awaiting = set(watch.get("awaiting_confirm") or [])
    excluded = set(watch.get("excluded_trains") or [])
    ts = datetime.now().strftime("%H:%M:%S")
    date_short = watch["date"][:10]

    # Decide which trains to act on this cycle.
    if explicit:
        active = sorted(explicit - excluded)
    else:
        active = sorted(set(train_avail.keys()) - excluded)

    any_seats = False
    for number in active:
        if number in awaiting:
            continue  # paused; user hasn't told us Keep or Stop yet
        info = train_avail.get(number)
        count = info.get("count", 0) if info else 0

        if count > 0:
            any_seats = True
            if number in notified:
                continue  # already alerted; keep watching for the sell-out transition
            depart = info.get("depart", "—") if info else "—"
            arrive = info.get("arrive", "—") if info else "—"
            train_id = info.get("train_id") if info else None

            hold = None
            if train_id is not None:
                try:
                    hold = try_hold_seat(
                        watch["from_id"], watch["to_id"], int(train_id),
                    )
                except Exception as e:
                    print(f"[hold] unexpected error: {e}", file=sys.stderr)

            if hold:
                wagon_idx = hold.get("wagon_index")
                wagon_label = (
                    str(wagon_idx + 1) if isinstance(wagon_idx, int) else "?"
                )
                msg = (
                    "🎫 *TCDD SEAT HELD* 🎫\n\n"
                    f"Train *{number}*  ({depart} → {arrive})\n"
                    f"{watch['from_name']} → {watch['to_name']}\n"
                    f"{date_short}\n"
                    f"Seat *{hold['seat']}*  (wagon {wagon_label})\n\n"
                    "*Complete payment within 10 minutes:*\n"
                    "https://ebilet.tcddtasimacilik.gov.tr/\n\n"
                    "_The seat will release automatically if not paid._"
                )
            else:
                msg = (
                    "🚨 *TCDD TICKET ALERT* 🚨\n\n"
                    f"Train *{number}*  ({depart} → {arrive})\n"
                    f"{watch['from_name']} → {watch['to_name']}\n"
                    f"{date_short}\n"
                    f"EKONOMİ seats available: *{count}*\n"
                    "_(auto-hold failed — book manually)_\n\n"
                    "Book now: https://ebilet.tcddtasimacilik.gov.tr/\n\n"
                    "_I'll keep watching this train. When it sells out, I'll ask "
                    "whether to continue._"
                )
            print(
                f"[{ts}] chat {chat_id} watch #{watch['id']} train {number} "
                f"({depart}→{arrive}): {count} seats — "
                f"{'held ' + hold['seat'] if hold else 'hold failed'}"
            )
            tg_send(chat_id, msg)
            mark_notified(chat_id, watch["id"], number)
        else:
            # count == 0
            if number in notified:
                # Transition P2 → P3: was alerted, just sold out. Ask the user.
                depart = info.get("depart", "—") if info else "—"
                arrive = info.get("arrive", "—") if info else "—"
                clear_notified(chat_id, watch["id"], number)
                mark_awaiting_confirm(chat_id, watch["id"], number)
                question = (
                    f"🟡 Train *{number}*  ({depart} → {arrive}) just sold out.\n"
                    f"{watch['from_name']} → {watch['to_name']}\n"
                    f"{date_short}\n\n"
                    "Did you book it? Should I keep watching in case more seats open up?"
                )
                print(
                    f"[{ts}] chat {chat_id} watch #{watch['id']} train {number}: "
                    f"sold out — asking user"
                )
                tg_send(chat_id, question, reply_markup=_keep_stop_keyboard(watch["id"], number))

    if not any_seats:
        trains_str = ",".join(active) if explicit else f"any({len(active)})"
        print(
            f"[{ts}] chat {chat_id} watch #{watch['id']} "
            f"({watch['from_name']}→{watch['to_name']} {date_short} trains={trains_str}): "
            f"no seats"
        )
