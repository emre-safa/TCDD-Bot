"""TCDD station catalog (read-only at runtime) and fuzzy matching."""
from __future__ import annotations

import re
import sys
import threading
import unicodedata

from tcdd_bot.config import STATIONS_FILE
from tcdd_bot.storage import read_json


_stations_lock = threading.RLock()


def load_stations() -> list[dict]:
    """Read the official station catalog. Read-only at runtime."""
    with _stations_lock:
        data = read_json(STATIONS_FILE, None)
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
