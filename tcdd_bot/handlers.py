"""Telegram command handlers, /add interactive flow, and update dispatch."""
from __future__ import annotations

import re
import sys
import threading
import traceback
from datetime import datetime

from tcdd_bot.config import CHECK_INTERVAL_SECONDS
from tcdd_bot.seat_hold import release_seat
from tcdd_bot.stations import find_stations, load_stations
from tcdd_bot.subscriptions import (
    add_watch,
    clear_awaiting_confirm,
    get_user,
    pop_hold,
    remove_watch,
    set_pause,
    stop_train_on_watch,
)
from tcdd_bot.tcdd_api import TcddAuthError, list_direct_trains, query_route
from tcdd_bot.telegram_api import (
    tg_answer_callback,
    tg_edit_message,
    tg_get_updates,
    tg_send,
)


# ─── In-memory /add flow state ──────────────────────────────────────────────

_state_lock = threading.RLock()
_states: dict[int, dict] = {}


def get_state(chat_id: int) -> dict | None:
    with _state_lock:
        return _states.get(chat_id)


def set_state(chat_id: int, state: dict | None) -> None:
    with _state_lock:
        if state is None:
            _states.pop(chat_id, None)
        else:
            _states[chat_id] = state


# ─── Command dispatch ───────────────────────────────────────────────────────

HELP_TEXT = (
    "*TCDD Availability Bot*\n\n"
    "I watch the TCDD ticket API and auto-hold a seat for you when one opens "
    "on the trains you choose. You'll have 10 minutes to complete payment.\n\n"
    "*Commands*\n"
    "/add — add a watch (interactive)\n"
    "/list — list your watches\n"
    "/remove `<N>` — remove watch #N\n"
    "/pause — pause notifications\n"
    "/resume — resume notifications\n"
    "/cancel — cancel an in-progress /add\n"
    "/help — show this help"
)


def handle_command(chat_id: int, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return

    lower = text.lower()

    # /cancel works regardless of state
    if lower == "/cancel" or lower == "cancel":
        if get_state(chat_id):
            set_state(chat_id, None)
            tg_send(chat_id, "Cancelled.")
        else:
            tg_send(chat_id, "Nothing to cancel.")
        return

    # If we're in an interactive flow, route there first
    state = get_state(chat_id)
    if state and not text.startswith("/"):
        continue_add_flow(chat_id, state, text)
        return
    if state and text.startswith("/"):
        # A new command interrupts the flow
        set_state(chat_id, None)

    if lower.startswith("/start") or lower.startswith("/help"):
        tg_send(chat_id, HELP_TEXT)
        return
    if lower == "/add" or lower.startswith("/add "):
        start_add_flow(chat_id)
        return
    if lower.startswith("/list"):
        cmd_list(chat_id)
        return
    if lower.startswith("/remove"):
        cmd_remove(chat_id, text)
        return
    if lower.startswith("/pause"):
        set_pause(chat_id, True)
        tg_send(chat_id, "Notifications paused. Use /resume to start again.")
        return
    if lower.startswith("/resume"):
        set_pause(chat_id, False)
        tg_send(chat_id, "Notifications resumed.")
        return

    tg_send(chat_id, "Unknown command. Send /help for the list.")


def cmd_list(chat_id: int) -> None:
    user = get_user(chat_id)
    watches = user.get("watches", [])
    if not watches:
        tg_send(chat_id, "You have no watches. Use /add to create one.")
        return
    paused = " *(paused)*" if user.get("paused") else ""
    lines = [f"*Your watches*{paused}", ""]
    for w in watches:
        trains = ", ".join(w.get("train_numbers") or []) or "any"
        lines.append(
            f"#{w['id']}  {w['from_name']} → {w['to_name']}\n"
            f"     {w['date'][:10]}   trains: {trains}"
        )
    tg_send(chat_id, "\n".join(lines))


def cmd_remove(chat_id: int, text: str) -> None:
    parts = text.split()
    if len(parts) < 2 or not parts[1].lstrip("#").isdigit():
        tg_send(chat_id, "Usage: `/remove <N>` — see /list for IDs.")
        return
    watch_id = int(parts[1].lstrip("#"))
    if remove_watch(chat_id, watch_id):
        tg_send(chat_id, f"Removed watch #{watch_id}.")
    else:
        tg_send(chat_id, f"No watch #{watch_id} found.")


# ─── /add interactive flow ──────────────────────────────────────────────────

def start_add_flow(chat_id: int) -> None:
    set_state(chat_id, {"step": "from", "data": {}})
    tg_send(
        chat_id,
        "Let's add a watch.\n\n"
        "*Step 1/4 — Departure station*\n"
        "Type a city name (e.g. `konya`) or its station ID (e.g. `796`).\n"
        "Send /cancel to abort.",
    )


def continue_add_flow(chat_id: int, state: dict, text: str) -> None:
    step = state["step"]
    data = state["data"]

    if step == "from":
        resolved = resolve_station_input(chat_id, text)
        if not resolved:
            return
        data["from_id"], data["from_name"] = resolved
        state["step"] = "to"
        set_state(chat_id, state)
        tg_send(
            chat_id,
            f"Departure: *{data['from_name']}* (#{data['from_id']}).\n\n"
            "*Step 2/4 — Arrival station*\n"
            "Type a city name or station ID.",
        )
        return

    if step == "to":
        resolved = resolve_station_input(chat_id, text)
        if not resolved:
            return
        if resolved[0] == data.get("from_id"):
            tg_send(chat_id, "Arrival can't equal departure. Try again.")
            return
        data["to_id"], data["to_name"] = resolved
        state["step"] = "date"
        set_state(chat_id, state)
        tg_send(
            chat_id,
            f"Arrival: *{data['to_name']}* (#{data['to_id']}).\n\n"
            "*Step 3/4 — Departure date*\n"
            "Format: `DD-MM-YYYY`, e.g. `21-03-2026`.",
        )
        return

    if step == "date":
        date_str = text.strip()
        if not re.match(r"^\d{2}-\d{2}-\d{4}$", date_str):
            tg_send(chat_id, "Invalid format. Use `DD-MM-YYYY`, e.g. `21-03-2026`.")
            return
        try:
            datetime.strptime(date_str, "%d-%m-%Y")
        except ValueError:
            tg_send(chat_id, "That date doesn't exist. Try again.")
            return
        data["date"] = date_str  # store as plain DD-MM-YYYY; API filler added on send

        tg_send(chat_id, f"Date: *{date_str}*. Looking up direct trains…")
        try:
            response = query_route(
                data["from_id"], data["from_name"],
                data["to_id"], data["to_name"],
                data["date"],
            )
        except TcddAuthError as e:
            tg_send(
                chat_id,
                f"❌ TCDD rejected our request (HTTP {e.status}). The bot's "
                f"`TCDD_AUTH_TOKEN` is missing or expired. Ask the operator "
                f"to refresh it; your watch was *not* saved.",
            )
            set_state(chat_id, None)
            return
        except Exception as e:
            tg_send(chat_id, f"❌ Couldn't reach TCDD ({e}). Try again in a moment.")
            set_state(chat_id, None)
            return

        direct = list_direct_trains(response, data["from_id"], data["to_id"])
        if not direct:
            tg_send(
                chat_id,
                f"No *direct* trains from {data['from_name']} to {data['to_name']} "
                f"on {date_str}.\n"
                f"Either no direct service runs this route, or the date is outside "
                f"TCDD's booking window (~1 month).\n"
                f"Send another date in `DD-MM-YYYY` format, or /cancel.",
            )
            return  # stay in the date step so the user can try a different date

        data["_available"] = [t["number"] for t in direct]
        state["step"] = "trains"
        set_state(chat_id, state)

        lines = [
            f"*Direct trains* on {date_str}, "
            f"{data['from_name']} → {data['to_name']}:",
            "",
        ]
        for t in direct:
            lines.append(
                f"• `{t['number']}`  {t['depart']} → {t['arrive']}   "
                f"_{t['commercial_name']}_"
            )
        lines += [
            "",
            "*Step 4/4 — Train numbers*",
            "Pick from the list above (e.g. `" + direct[0]["number"] + "`), "
            "comma-separate multiple, or send `any` to watch them all.",
        ]
        tg_send(chat_id, "\n".join(lines))
        return

    if step == "trains":
        raw = text.strip()
        available: list[str] = data.get("_available") or []
        if raw.lower() in ("any", "*", "all"):
            train_numbers: list[str] = []
        else:
            train_numbers = [t.strip() for t in re.split(r"[,\s]+", raw) if t.strip()]
            if not train_numbers:
                tg_send(chat_id, "Provide at least one train number, or `any`.")
                return
            invalid = [n for n in train_numbers if n not in set(available)]
            if invalid:
                tg_send(
                    chat_id,
                    f"These don't run "
                    f"{data['from_name']} → {data['to_name']} directly: "
                    f"*{', '.join(invalid)}*.\n\n"
                    f"Valid options: {', '.join(available)}\n\n"
                    f"Re-enter, or send `any`.",
                )
                return

        data.pop("_available", None)
        data["train_numbers"] = train_numbers
        watch_id = add_watch(chat_id, data)
        set_state(chat_id, None)
        trains_str = (
            ", ".join(train_numbers)
            if train_numbers
            else f"any ({len(available)} direct trains)"
        )
        tg_send(
            chat_id,
            f"✅ Watch #{watch_id} saved.\n\n"
            f"{data['from_name']} → {data['to_name']}\n"
            f"{data['date'][:10]}\n"
            f"Trains: {trains_str}\n\n"
            f"I'll check every {CHECK_INTERVAL_SECONDS}s. When an EKONOMİ "
            f"seat opens, I'll auto-hold one and message you to pay within "
            f"10 minutes.",
        )
        return


def resolve_station_input(chat_id: int, text: str) -> tuple[int, str] | None:
    """Resolve user input against the official catalog. Returns (id, name) or None.

    Accepts either a numeric station ID present in the catalog, or a name
    substring that matches exactly one catalog entry. Anything else prompts
    the user to retry — there is no way to invent stations on the fly.
    """
    text = text.strip()
    if not text:
        tg_send(chat_id, "Empty input. Try again or /cancel.")
        return None

    if text.isdigit():
        sid = int(text)
        match = next((s for s in load_stations() if s["id"] == sid), None)
        if match:
            return match["id"], match["name"]
        tg_send(chat_id, f"No station with ID #{sid} in the catalog. Try a name.")
        return None

    matches = find_stations(text)
    if len(matches) == 1:
        return matches[0]["id"], matches[0]["name"]
    if len(matches) > 1:
        lines = ["Multiple matches — pick one by sending its ID:"]
        for s in matches[:20]:
            lines.append(f"`{s['id']}` — {s['name']}")
        if len(matches) > 20:
            lines.append(f"…and {len(matches) - 20} more. Refine your search.")
        tg_send(chat_id, "\n".join(lines))
        return None

    tg_send(
        chat_id,
        f"No station matches '{text}'. Try a different spelling or a "
        f"more specific city name.",
    )
    return None


# ─── Telegram update dispatch ───────────────────────────────────────────────

def telegram_loop() -> None:
    print("[bot] started")
    offset = 0
    while True:
        updates = tg_get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            try:
                process_update(update)
            except Exception:
                print("[bot] update error:", file=sys.stderr)
                traceback.print_exc()


def process_update(update: dict) -> None:
    cb = update.get("callback_query")
    if cb:
        handle_callback(cb)
        return
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = msg.get("text")
    if chat_id is None or text is None:
        return
    handle_command(int(chat_id), text)


def handle_callback(cb: dict) -> None:
    """Handle clicks on the Keep / Stop buttons on a sold-out question."""
    cb_id = cb.get("id", "")
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    if chat_id is None or not data:
        tg_answer_callback(cb_id)
        return

    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] not in ("k", "s", "r") or not parts[1].isdigit():
        tg_answer_callback(cb_id, "Invalid action.")
        return
    action, watch_id_str, train_number = parts
    watch_id = int(watch_id_str)
    chat_id = int(chat_id)

    if action == "k":
        clear_awaiting_confirm(chat_id, watch_id, train_number)
        tg_answer_callback(cb_id, "Still watching")
        if message_id is not None:
            tg_edit_message(
                chat_id,
                int(message_id),
                f"✅ Still watching train *{train_number}*. "
                f"I'll alert you again if seats reopen.",
            )
        return

    if action == "r":
        # Pop first so a second click can't double-fire the release call.
        info = pop_hold(chat_id, watch_id, train_number)
        if not info:
            tg_answer_callback(cb_id, "No active hold")
            if message_id is not None:
                tg_edit_message(
                    chat_id,
                    int(message_id),
                    f"ℹ️ No active hold on train *{train_number}* "
                    f"(already released or expired).",
                )
            return
        ok = release_seat(
            int(info["train_car_id"]),
            str(info["allocation_id"]),
            str(info["seat_number"]),
        )
        if ok:
            tg_answer_callback(cb_id, "Released")
            body = (
                f"🔓 Seat *{info['seat_number']}* released on train "
                f"*{train_number}*.\n\nBook it now: "
                f"https://ebilet.tcddtasimacilik.gov.tr/"
            )
        else:
            tg_answer_callback(cb_id, "Release failed")
            body = (
                f"⚠️ Couldn't release seat *{info['seat_number']}* on train "
                f"*{train_number}* — TCDD rejected the request. The hold may "
                f"have already expired."
            )
        if message_id is not None:
            tg_edit_message(chat_id, int(message_id), body)
        return

    # action == "s"
    result = stop_train_on_watch(chat_id, watch_id, train_number)
    tg_answer_callback(cb_id, "Stopped")
    status = result.get("status")
    if status == "watch_dropped":
        body = (
            f"🛑 Stopped watching train *{train_number}* — watch #{watch_id} had "
            f"no other trains, so I removed it."
        )
    elif status == "explicit_removed":
        remaining = ", ".join(result.get("remaining") or [])
        body = (
            f"🛑 Stopped watching train *{train_number}*. "
            f"Still watching on watch #{watch_id}: {remaining}."
        )
    elif status == "excluded":
        body = (
            f"🛑 Stopped watching train *{train_number}*. Other trains on this "
            f"route stay monitored."
        )
    else:
        body = f"🛑 Couldn't find watch #{watch_id}."
    if message_id is not None:
        tg_edit_message(chat_id, int(message_id), body)
