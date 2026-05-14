"""Per-user watch persistence and mutations.

A "subscription" is one Telegram user's record in subscriptions.json:
  {paused, next_id, watches: [{id, from_id, ..., notified_trains,
                               awaiting_confirm, excluded_trains}]}
"""
from __future__ import annotations

import threading

from tcdd_bot.config import SUBS_FILE
from tcdd_bot.storage import read_json, write_json


_subs_lock = threading.RLock()


def load_subs() -> dict:
    with _subs_lock:
        return read_json(SUBS_FILE, {})


def save_subs(data: dict) -> None:
    with _subs_lock:
        write_json(SUBS_FILE, data)


def migrate_subscription_dates() -> None:
    """One-time cleanup: strip the legacy ' 00:00:00' suffix from stored dates."""
    with _subs_lock:
        subs = read_json(SUBS_FILE, None)
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
            write_json(SUBS_FILE, subs)
            print("[migrate] trimmed legacy time suffix from subscriptions.json dates")


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
