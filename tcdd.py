#!/usr/bin/env python3
"""TCDD train availability notifier — multi-user Telegram bot.

Each Telegram user registers one or more "watches": a route, a date, and an
optional list of train numbers. A background worker polls the TCDD
availability API and messages the user when an EKONOMİ seat opens on any
of their watched trains.

Setup
-----
  export TELEGRAM_BOT_TOKEN="<your bot token>"        # or edit BOT_TOKEN below
  export TCDD_AUTH_TOKEN="<bearer token, optional>"   # leave empty if not needed
  python tcdd.py

Telegram commands (send to the bot in chat)
-------------------------------------------
  /start, /help        Show usage
  /add                 Start interactive watch creation
  /list                List your watches
  /remove <N>          Remove watch #N
  /pause, /resume      Pause / resume your notifications
  /cancel              Abort an in-progress /add

The station catalog lives in stations.json (official IDs sourced from TCDD)
and is read-only at runtime — there is no Telegram command to modify it.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
import time
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


# ─── Configuration ──────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TCDD_AUTH = os.environ.get("TCDD_AUTH_TOKEN", "").strip()

if not BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN environment variable is not set")
if not TCDD_AUTH:
    raise SystemExit("TCDD_AUTH_TOKEN environment variable is not set")

CHECK_INTERVAL_SECONDS = 60
TELEGRAM_LONG_POLL_TIMEOUT = 25
PER_API_CALL_DELAY = 1.5  # be polite to the TCDD endpoint

BASE_DIR = Path(__file__).resolve().parent
SUBS_FILE = BASE_DIR / "subscriptions.json"
STATIONS_FILE = BASE_DIR / "stations.json"

TCDD_URL = (
    "https://web-api-prod-ytp.tcddtasimacilik.gov.tr"
    "/tms/train/train-availability?environment=dev&userId=1"
)
TARGET_CABIN_CLASS = "EKONOMİ"

# All TCDD timestamps in segments[].departureTime / arrivalTime are UTC epoch ms.
# Display them in Türkiye time (GMT+03:00, no DST since 2016).
DISPLAY_TZ = timezone(timedelta(hours=3))


# ─── JSON storage helpers ───────────────────────────────────────────────────

_subs_lock = threading.RLock()
_stations_lock = threading.RLock()


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_subs() -> dict:
    with _subs_lock:
        return _read_json(SUBS_FILE, {})


def save_subs(data: dict) -> None:
    with _subs_lock:
        _write_json(SUBS_FILE, data)


def _migrate_subscription_dates() -> None:
    """One-time cleanup: strip the legacy ' 00:00:00' suffix from stored dates."""
    with _subs_lock:
        subs = _read_json(SUBS_FILE, None)
        if not subs:
            return
        changed = False
        for user in subs.values():
            for w in user.get("watches") or []:
                d = w.get("date") or ""
                if len(d) > 10:
                    w["date"] = d[:10]
                    changed = True
        if changed:
            _write_json(SUBS_FILE, subs)
            print("[migrate] trimmed legacy time suffix from subscriptions.json dates")


def load_stations() -> list[dict]:
    """Read the official station catalog. Read-only at runtime."""
    with _stations_lock:
        data = _read_json(STATIONS_FILE, None)
        if data is None:
            print(
                f"ERROR: station catalog missing at {STATIONS_FILE}. "
                f"Cannot resolve station names without it.",
                file=sys.stderr,
            )
            return []
        return data


def _normalize_station_text(s: str) -> str:
    """Loose key for fuzzy station matching.

    casefold + transliterate Turkish-specific letters (ı, İ, ş, ç, ğ, ö, ü) to
    plain ASCII via NFD decomposition + strip combining marks + drop all
    whitespace/punctuation. So "Istanbul (sogutlucesme)", "İSTANBUL(SÖĞÜTLÜÇEŞME)",
    and "istanbulsogutlucesme" all collapse to the same key.
    """
    if not s:
        return ""
    # casefold lowercases İ to "i + combining dot" but leaves ı (dotless) alone.
    s = s.casefold().translate(str.maketrans({"ı": "i"}))
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE)


def find_stations(query: str, limit: int = 10) -> list[dict]:
    q = _normalize_station_text(query)
    stations = load_stations()
    if not q:
        return stations[:limit]
    return [s for s in stations if q in _normalize_station_text(s["name"])][:limit]


# ─── Telegram low-level HTTP ────────────────────────────────────────────────

_TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


def tg_send(
    chat_id: int,
    text: str,
    parse_mode: str | None = "Markdown",
    reply_markup: dict | None = None,
) -> None:
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(f"{_TG_BASE}/sendMessage", json=payload, timeout=15)
        if resp.status_code != 200 and parse_mode:
            # Markdown rejection — retry as plain text so the message still lands.
            retry = {"chat_id": chat_id, "text": text}
            if reply_markup is not None:
                retry["reply_markup"] = reply_markup
            requests.post(f"{_TG_BASE}/sendMessage", json=retry, timeout=15)
    except Exception as e:
        print(f"[tg_send] failed for chat {chat_id}: {e}", file=sys.stderr)


def tg_answer_callback(callback_id: str, text: str | None = None) -> None:
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    try:
        requests.post(f"{_TG_BASE}/answerCallbackQuery", json=payload, timeout=10)
    except Exception as e:
        print(f"[tg_answer_callback] {e}", file=sys.stderr)


def tg_edit_message(chat_id: int, message_id: int, text: str, parse_mode: str | None = "Markdown") -> None:
    """Replace the text of an existing message and drop its inline keyboard."""
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        resp = requests.post(f"{_TG_BASE}/editMessageText", json=payload, timeout=15)
        if resp.status_code != 200 and parse_mode:
            requests.post(
                f"{_TG_BASE}/editMessageText",
                json={"chat_id": chat_id, "message_id": message_id, "text": text},
                timeout=15,
            )
    except Exception as e:
        print(f"[tg_edit_message] {e}", file=sys.stderr)


def tg_get_updates(offset: int) -> list[dict]:
    try:
        resp = requests.get(
            f"{_TG_BASE}/getUpdates",
            params={
                "timeout": TELEGRAM_LONG_POLL_TIMEOUT,
                "offset": offset,
                "allowed_updates": json.dumps(["message", "edited_message", "callback_query"]),
            },
            timeout=TELEGRAM_LONG_POLL_TIMEOUT + 10,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as e:
        print(f"[tg_get_updates] failed: {e}", file=sys.stderr)
        time.sleep(3)
        return []


# ─── TCDD API ───────────────────────────────────────────────────────────────

def decode_jwt_claims(token: str) -> dict | None:
    """Return the decoded payload of a JWT (no signature check). None on failure."""
    t = (token or "").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    parts = t.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload + padding))
    except Exception:
        return None


def describe_jwt(token: str) -> str:
    """Human-readable summary of a JWT's TTL — for the startup banner."""
    claims = decode_jwt_claims(token)
    if not claims:
        return "configured (not a decodable JWT — sent as opaque token)"
    exp = claims.get("exp")
    iat = claims.get("iat")
    now = int(time.time())
    if exp is None:
        return "configured (JWT has no `exp` claim)"
    if now > exp:
        # In practice TCDD has been observed to accept stale tokens as long as the
        # browser-like headers are present, but undocumented behavior can change.
        days = (now - exp) // 86400
        ago = f"{days}d ago" if days >= 1 else f"{now - exp}s ago"
        return f"expired {ago} (TCDD has been seen accepting stale tokens; refresh if calls fail)"
    ttl = exp - now
    lifetime = (exp - iat) if iat else None
    extra = f", lifetime {lifetime}s" if lifetime else ""
    return f"valid for {ttl}s more{extra}"


def tcdd_headers() -> dict:
    """Mimic the browser's request as closely as possible — TCDD's WAF rejects
    requests missing the sec-ch-ua / Sec-Fetch family of headers.
    """
    h = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "tr",
        "Connection": "keep-alive",
        "Content-Type": "application/json",
        "Origin": "https://ebilet.tcddtasimacilik.gov.tr",
        "Referer": "https://ebilet.tcddtasimacilik.gov.tr/",
        "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "unit-id": "3895",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        ),
    }
    if TCDD_AUTH:
        h["Authorization"] = TCDD_AUTH
    return h


_logged_auth_statuses: set[int] = set()


def _log_auth_failure(status: int) -> None:
    """Print a one-shot detailed auth error message per status code."""
    if status in _logged_auth_statuses:
        print(f"[tcdd] HTTP {status} — auth still failing.", file=sys.stderr)
        return
    _logged_auth_statuses.add(status)
    if status == 401:
        print(
            "[tcdd] 401 Unauthorized — TCDD_AUTH_TOKEN is missing or empty.\n"
            "  Capture a bearer token from a current TCDD browser session:\n"
            "    1. Open https://ebilet.tcddtasimacilik.gov.tr/ and run any search\n"
            "    2. F12 → Network tab → click the 'train-availability' request\n"
            "    3. Copy the entire 'Authorization' request header value\n"
            "    4. export TCDD_AUTH_TOKEN='<that value>' and restart this script",
            file=sys.stderr,
        )
    elif status == 403:
        print(
            "[tcdd] 403 Forbidden — TCDD rejected your bearer token (likely expired).\n"
            "  TCDD JWTs are short-lived. Capture a fresh one from a current\n"
            "  browser session (F12 → Network → train-availability request →\n"
            "  Authorization header), re-export TCDD_AUTH_TOKEN, and restart.",
            file=sys.stderr,
        )


class TcddAuthError(Exception):
    """TCDD returned 401/403 — propagated so callers can surface it to the user."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


def _api_date(d: str) -> str:
    """Build the TCDD API's `DD-MM-YYYY HH:MM:SS` date string from any stored form.

    The HH:MM:SS portion is a TCDD-mandated filler (the search is per-day); we
    keep it out of subscriptions.json and synthesize it only at request time.
    """
    d = (d or "").strip()
    return d if len(d) > 10 else f"{d} 00:00:00"


def _availability_payload(from_id: int, from_name: str, to_id: int, to_name: str, date: str) -> dict:
    return {
        "passengerTypeCounts": [{"id": 0, "count": 1}],
        "searchReservation": False,
        "searchRoutes": [
            {
                "departureStationId": from_id,
                "departureStationName": from_name,
                "arrivalStationId": to_id,
                "arrivalStationName": to_name,
                "departureDate": _api_date(date),
            }
        ],
    }


TCDD_HTTP_TIMEOUT = 30           # seconds per attempt
TCDD_HTTP_RETRIES = 1            # extra attempts after the first on read timeout
TCDD_HTTP_RETRY_DELAY = 1.0      # seconds between attempts


def _post_availability(payload: dict) -> dict:
    """POST to the train-availability endpoint with one retry on timeout.

    Raises:
        TcddAuthError on 401/403.
        requests.exceptions.* (Timeout / ConnectionError / HTTPError) on
        unrecoverable failures — callers decide whether to swallow or surface.
    """
    last_timeout: Exception | None = None
    for attempt in range(TCDD_HTTP_RETRIES + 1):
        try:
            resp = requests.post(
                TCDD_URL, json=payload, headers=tcdd_headers(),
                timeout=TCDD_HTTP_TIMEOUT,
            )
            if resp.status_code in (401, 403):
                _log_auth_failure(resp.status_code)
                raise TcddAuthError(resp.status_code)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout as e:
            last_timeout = e
            if attempt < TCDD_HTTP_RETRIES:
                time.sleep(TCDD_HTTP_RETRY_DELAY)
                continue
            raise
    # Defensive — the loop either returns or raises; this should be unreachable.
    raise last_timeout  # type: ignore[misc]


def query_availability(watch: dict) -> dict | None:
    payload = _availability_payload(
        watch["from_id"], watch["from_name"],
        watch["to_id"], watch["to_name"],
        watch["date"],
    )
    try:
        return _post_availability(payload)
    except TcddAuthError:
        return None  # already logged by _log_auth_failure
    except Exception as e:
        print(
            f"[tcdd] {watch['from_name']}→{watch['to_name']} {watch['date'][:10]}: {e}",
            file=sys.stderr,
        )
        return None


def query_route(from_id: int, from_name: str, to_id: int, to_name: str, date: str) -> dict:
    """Like query_availability, but lets exceptions propagate so /add can react."""
    return _post_availability(_availability_payload(from_id, from_name, to_id, to_name, date))


def _fmt_train_time(ms) -> str:
    """Format a TCDD UTC-epoch-ms timestamp as HH:MM in Türkiye time (GMT+03:00)."""
    if ms is None:
        return "—"
    return datetime.fromtimestamp(int(ms) / 1000, tz=DISPLAY_TZ).strftime("%H:%M")


def _iter_direct_trains(data: dict, from_id: int, to_id: int):
    """Yield every direct-service train object matching the user's exact route.

    'Direct' means the response presents the journey as a single train. Multi-train
    entries inside `trainAvailabilities` are transfer combos (e.g. İstanbul-Konya
    on YHT then Konya-Karaman on a regional), where the second leg's train neither
    starts at the user's departure nor ends at their arrival — those leaked into the
    earlier "suggested trains" list and produced the 81273 false suggestion.
    """
    legs = data.get("trainLegs") or []
    if not legs:
        return
    for av in legs[0].get("trainAvailabilities") or []:
        trains = av.get("trains") or []
        if len(trains) != 1:
            continue
        t = trains[0]
        if t.get("departureStationId") != from_id:
            continue
        if t.get("arrivalStationId") != to_id:
            continue
        yield t


def list_direct_trains(data: dict, from_id: int, to_id: int) -> list[dict]:
    """Return a sorted list of direct trains as {number, commercial_name, depart, arrive}."""
    out: list[dict] = []
    seen: set[str] = set()
    for t in _iter_direct_trains(data, from_id, to_id):
        number = str(t.get("number", "")).strip()
        if not number or number in seen:
            continue
        seen.add(number)
        segs = t.get("segments") or []
        depart_ms = segs[0].get("departureTime") if segs else None
        arrive_ms = segs[-1].get("arrivalTime") if segs else None
        out.append({
            "number": number,
            "commercial_name": t.get("commercialName") or "",
            "depart": _fmt_train_time(depart_ms),
            "arrive": _fmt_train_time(arrive_ms),
            "depart_ms": depart_ms or 0,
        })
    out.sort(key=lambda x: x["depart_ms"])
    return out


def _ekonomi_count_for_train(train: dict) -> int:
    """EKONOMİ seat count for the full origin→destination journey.

    Uses the aggregated availableFareInfo[].cabinClasses[] block, which the
    backend has already validated across all mid-segments.
    """
    best = 0
    for fare in train.get("availableFareInfo") or []:
        for cabin in fare.get("cabinClasses") or []:
            name = (cabin.get("cabinClass") or {}).get("name", "")
            if name == TARGET_CABIN_CLASS:
                count = int(cabin.get("availabilityCount") or 0)
                if count > best:
                    best = count
    return best


def collect_train_availability(data: dict, from_id: int, to_id: int) -> dict[str, dict]:
    """Return {train_number: {count, depart, arrive, commercial_name}} for direct trains."""
    out: dict[str, dict] = {}
    for t in _iter_direct_trains(data, from_id, to_id):
        number = str(t.get("number", "")).strip()
        if not number:
            continue
        count = _ekonomi_count_for_train(t)
        segs = t.get("segments") or []
        depart_ms = segs[0].get("departureTime") if segs else None
        arrive_ms = segs[-1].get("arrivalTime") if segs else None
        existing = out.get(number)
        if existing is None or count > existing["count"]:
            out[number] = {
                "count": count,
                "depart": _fmt_train_time(depart_ms),
                "arrive": _fmt_train_time(arrive_ms),
                "commercial_name": t.get("commercialName") or "",
            }
    return out


# ─── Subscription / watch model ─────────────────────────────────────────────

def get_user(chat_id: int) -> dict:
    subs = load_subs()
    user = subs.get(str(chat_id))
    if not user:
        user = {"paused": False, "next_id": 1, "watches": []}
        subs[str(chat_id)] = user
        save_subs(subs)
    return user


def add_watch(chat_id: int, watch: dict) -> int:
    subs = load_subs()
    user = subs.setdefault(str(chat_id), {"paused": False, "next_id": 1, "watches": []})
    watch_id = int(user.get("next_id", 1))
    watch["id"] = watch_id
    watch.setdefault("notified_trains", [])   # phase 2: alerted & seats still available
    watch.setdefault("awaiting_confirm", [])  # phase 3: sold out, waiting for user keep/stop
    watch.setdefault("excluded_trains", [])   # 'any'-mode trains the user explicitly stopped
    user["next_id"] = watch_id + 1
    user["watches"].append(watch)
    save_subs(subs)
    return watch_id


def remove_watch(chat_id: int, watch_id: int) -> bool:
    subs = load_subs()
    user = subs.get(str(chat_id))
    if not user:
        return False
    before = len(user["watches"])
    user["watches"] = [w for w in user["watches"] if int(w.get("id", 0)) != watch_id]
    if len(user["watches"]) == before:
        return False
    save_subs(subs)
    return True


def set_pause(chat_id: int, paused: bool) -> None:
    subs = load_subs()
    user = subs.setdefault(str(chat_id), {"paused": False, "next_id": 1, "watches": []})
    user["paused"] = paused
    save_subs(subs)


def _mutate_notified(chat_id: int, watch_id: int, train_number: str, add: bool) -> None:
    subs = load_subs()
    user = subs.get(str(chat_id))
    if not user:
        return
    for w in user["watches"]:
        if int(w.get("id", 0)) != watch_id:
            continue
        notified = set(w.get("notified_trains") or [])
        if add:
            notified.add(train_number)
        else:
            notified.discard(train_number)
        w["notified_trains"] = sorted(notified)
        break
    save_subs(subs)


def mark_notified(chat_id: int, watch_id: int, train_number: str) -> None:
    _mutate_notified(chat_id, watch_id, train_number, add=True)


def clear_notified(chat_id: int, watch_id: int, train_number: str) -> None:
    _mutate_notified(chat_id, watch_id, train_number, add=False)


def _mutate_field(chat_id: int, watch_id: int, field: str, train_number: str, add: bool) -> None:
    subs = load_subs()
    user = subs.get(str(chat_id))
    if not user:
        return
    for w in user["watches"]:
        if int(w.get("id", 0)) != watch_id:
            continue
        cur = set(w.get(field) or [])
        if add:
            cur.add(train_number)
        else:
            cur.discard(train_number)
        w[field] = sorted(cur)
        break
    save_subs(subs)


def mark_awaiting_confirm(chat_id: int, watch_id: int, train_number: str) -> None:
    _mutate_field(chat_id, watch_id, "awaiting_confirm", train_number, add=True)


def clear_awaiting_confirm(chat_id: int, watch_id: int, train_number: str) -> None:
    _mutate_field(chat_id, watch_id, "awaiting_confirm", train_number, add=False)


def stop_train_on_watch(chat_id: int, watch_id: int, train_number: str) -> dict:
    """Drop a train from a watch following a 'Stop' button.

    Returns one of:
      {"status": "no_watch"}                       — watch doesn't exist
      {"status": "explicit_removed", "remaining": [...]}    — was in train_numbers, removed
      {"status": "watch_dropped"}                  — last explicit train removed → watch deleted
      {"status": "excluded"}                       — 'any' mode, added to excluded_trains
    """
    subs = load_subs()
    user = subs.get(str(chat_id))
    if not user:
        return {"status": "no_watch"}
    for idx, w in enumerate(user["watches"]):
        if int(w.get("id", 0)) != watch_id:
            continue
        # Always clear awaiting state for this train
        awaiting = [t for t in (w.get("awaiting_confirm") or []) if t != train_number]
        w["awaiting_confirm"] = awaiting
        notified = [t for t in (w.get("notified_trains") or []) if t != train_number]
        w["notified_trains"] = notified
        explicit = list(w.get("train_numbers") or [])
        if explicit:
            new_list = [t for t in explicit if t != train_number]
            w["train_numbers"] = new_list
            if not new_list:
                user["watches"].pop(idx)
                save_subs(subs)
                return {"status": "watch_dropped"}
            save_subs(subs)
            return {"status": "explicit_removed", "remaining": new_list}
        # 'any' mode — add to excluded
        excluded = set(w.get("excluded_trains") or [])
        excluded.add(train_number)
        w["excluded_trains"] = sorted(excluded)
        save_subs(subs)
        return {"status": "excluded"}
    return {"status": "no_watch"}


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


# ─── Telegram command handlers ──────────────────────────────────────────────

HELP_TEXT = (
    "*TCDD Availability Bot*\n\n"
    "I watch the TCDD ticket API and message you when an EKONOMİ seat opens "
    "on the trains you choose.\n\n"
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
            f"I'll check every {CHECK_INTERVAL_SECONDS}s and message you when "
            f"EKONOMİ seats open.",
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


# ─── Background poller ──────────────────────────────────────────────────────

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
            msg = (
                "🚨 *TCDD TICKET ALERT* 🚨\n\n"
                f"Train *{number}*  ({depart} → {arrive})\n"
                f"{watch['from_name']} → {watch['to_name']}\n"
                f"{date_short}\n"
                f"EKONOMİ seats available: *{count}*\n\n"
                "Book now: https://ebilet.tcddtasimacilik.gov.tr/\n\n"
                "_I'll keep watching this train. When it sells out, I'll ask "
                "whether to continue._"
            )
            print(
                f"[{ts}] chat {chat_id} watch #{watch['id']} train {number} "
                f"({depart}→{arrive}): {count} seats — notifying"
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


# ─── Telegram polling loop ──────────────────────────────────────────────────

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
    if len(parts) != 3 or parts[0] not in ("k", "s") or not parts[1].isdigit():
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


# ─── Entry point ────────────────────────────────────────────────────────────

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
            json=_availability_payload(from_id, from_name, to_id, to_name, test_date),
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

    _migrate_subscription_dates()

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


if __name__ == "__main__":
    main()
