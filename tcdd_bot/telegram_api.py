"""Low-level Telegram Bot API HTTP."""
from __future__ import annotations

import json
import sys
import time

import requests

from tcdd_bot.config import BOT_TOKEN, TELEGRAM_LONG_POLL_TIMEOUT


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
