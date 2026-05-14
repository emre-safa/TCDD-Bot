"""TCDD HTTP client — JWT helpers, headers, train-availability parsing."""
from __future__ import annotations

import base64
import json
import sys
import time
from datetime import datetime

import requests

from tcdd_bot.config import (
    DISPLAY_TZ,
    TARGET_CABIN_CLASS,
    TCDD_AUTH,
    TCDD_HTTP_RETRIES,
    TCDD_HTTP_RETRY_DELAY,
    TCDD_HTTP_TIMEOUT,
    TCDD_URL,
)


class TcddAuthError(Exception):
    """TCDD returned 401/403 — propagated so callers can surface it to the user."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


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


def _api_date(d: str) -> str:
    """Build the TCDD API's `DD-MM-YYYY HH:MM:SS` date string from any stored form.

    The HH:MM:SS portion is a TCDD-mandated filler (the search is per-day); we
    keep it out of subscriptions.json and synthesize it only at request time.
    """
    d = (d or "").strip()
    return d if len(d) > 10 else f"{d} 00:00:00"


def availability_payload(from_id: int, from_name: str, to_id: int, to_name: str, date: str) -> dict:
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


def post_tcdd_json(url: str, payload: dict) -> dict:
    """POST JSON to any TCDD endpoint with one retry on timeout.

    Raises:
        TcddAuthError on 401/403.
        requests.exceptions.* (Timeout / ConnectionError / HTTPError) on
        unrecoverable failures — callers decide whether to swallow or surface.
    """
    last_timeout: Exception | None = None
    for attempt in range(TCDD_HTTP_RETRIES + 1):
        try:
            resp = requests.post(
                url, json=payload, headers=tcdd_headers(),
                timeout=TCDD_HTTP_TIMEOUT,
            )
            if resp.status_code in (401, 403):
                _log_auth_failure(resp.status_code)
                raise TcddAuthError(resp.status_code)
            resp.raise_for_status()
            # release-seat returns 2xx with an empty body; treat that as {} so
            # callers don't have to distinguish "no JSON" from "rejected".
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {}
        except requests.exceptions.Timeout as e:
            last_timeout = e
            if attempt < TCDD_HTTP_RETRIES:
                time.sleep(TCDD_HTTP_RETRY_DELAY)
                continue
            raise
    # Defensive — the loop either returns or raises; this should be unreachable.
    raise last_timeout  # type: ignore[misc]


def _post_availability(payload: dict) -> dict:
    return post_tcdd_json(TCDD_URL, payload)


def query_availability(watch: dict) -> dict | None:
    payload = availability_payload(
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
    return _post_availability(availability_payload(from_id, from_name, to_id, to_name, date))


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


def _ekonomi_cabin_class_id(train: dict) -> int | None:
    """The numeric cabinClassId for EKONOMİ on this train, or None.

    Crosses cabin.cabinClass.name (string) from train-availability with the
    cabinClassId (int) used by load-by-train-id and select-seat. Without it
    the seat picker can't tell EKONOMİ wagons apart from business/disabled.
    """
    for fare in train.get("availableFareInfo") or []:
        for cabin in fare.get("cabinClasses") or []:
            cc = cabin.get("cabinClass") or {}
            if cc.get("name") == TARGET_CABIN_CLASS:
                cid = cc.get("id")
                if cid is not None:
                    return int(cid)
    return None


def collect_train_availability(data: dict, from_id: int, to_id: int) -> dict[str, dict]:
    """Return {train_number: {count, depart, arrive, commercial_name, train_id}} for direct trains."""
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
                "train_id": t.get("id"),
                "cabin_class_id": _ekonomi_cabin_class_id(t),
            }
    return out
