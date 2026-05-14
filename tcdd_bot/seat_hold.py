"""Seat-hold orchestration — load seat maps → pick a free seat → lock it.

A successful lock puts the seat in the basket and starts TCDD's 10-minute
countdown. The hold is tied to the account behind TCDD_AUTH_TOKEN.

release_seat() lets the user hand the seat back to TCDD before that 10-minute
window expires — typically because they want to buy it themselves rather than
wait for the auto-release.
"""
from __future__ import annotations

import sys

import requests

from tcdd_bot.config import (
    BOOKING_GENDER,
    TCDD_LOAD_BY_TRAIN_URL,
    TCDD_RELEASE_SEAT_URL,
    TCDD_SELECT_SEAT_URL,
)
from tcdd_bot.tcdd_api import TcddAuthError, post_tcdd_json


def load_seat_maps(from_id: int, to_id: int, train_id: int, leg_index: int = 0) -> dict | None:
    """Fetch the per-wagon seat layout for one train. None on any failure."""
    payload = {
        "fromStationId": from_id,
        "toStationId": to_id,
        "trainId": train_id,
        "legIndex": leg_index,
    }
    try:
        return post_tcdd_json(TCDD_LOAD_BY_TRAIN_URL, payload)
    except TcddAuthError:
        return None
    except requests.exceptions.HTTPError as e:
        body = ""
        status: int | str = "?"
        if e.response is not None:
            status = e.response.status_code
            try:
                body = e.response.text[:300]
            except Exception:
                pass
        print(
            f"[tcdd] load-by-train-id failed train={train_id}: HTTP {status} {body}",
            file=sys.stderr,
        )
        return None
    except Exception as e:
        print(f"[tcdd] load-by-train-id error train={train_id}: {e}", file=sys.stderr)
        return None


def pick_free_seat(seat_maps_data: dict, cabin_class_id: int) -> dict | None:
    """Return the first bookable seat in the requested cabin class, or None.

    A seat is bookable when it appears in seatPrices with the matching
    cabinClassId and is not in allocationSeats. Class-filtering is what keeps
    us from holding a business or disabled seat when the user wants EKONOMİ.

    Gender restrictions on free seats aren't surfaced in this response; if the
    select-seat POST is rejected for that reason, the hold fails and the user
    gets the fallback alert instead of a held seat.
    """
    for wagon in seat_maps_data.get("seatMaps") or []:
        if not wagon.get("trainCarOnSale"):
            continue
        taken = {s.get("seatNumber") for s in wagon.get("allocationSeats") or []}
        for sp in wagon.get("seatPrices") or []:
            if sp.get("cabinClassId") != cabin_class_id:
                continue
            seat_no = sp.get("seatNumber")
            if seat_no and seat_no not in taken:
                return {
                    "train_car_id": wagon.get("trainCarId"),
                    "train_car_index": wagon.get("trainCarIndex"),
                    "seat_number": seat_no,
                }
    return None


def select_seat(train_car_id: int, from_id: int, to_id: int, seat_number: str) -> dict | None:
    """POST the hold payload to lock the seat in the basket. None on failure."""
    payload = {
        "trainCarId": train_car_id,
        "fromStationId": from_id,
        "toStationId": to_id,
        "gender": BOOKING_GENDER,
        "seatNumber": seat_number,
        "passengerTypeId": 0,
        "totalPassengerCount": 1,
        "fareFamilyId": 0,
    }
    try:
        return post_tcdd_json(TCDD_SELECT_SEAT_URL, payload)
    except TcddAuthError:
        return None
    except requests.exceptions.HTTPError as e:
        body = ""
        status: int | str = "?"
        if e.response is not None:
            status = e.response.status_code
            try:
                body = e.response.text[:300]
            except Exception:
                pass
        print(
            f"[tcdd] select-seat rejected wagon={train_car_id} seat={seat_number}: "
            f"HTTP {status} {body}",
            file=sys.stderr,
        )
        return None
    except Exception as e:
        print(f"[tcdd] select-seat error: {e}", file=sys.stderr)
        return None


def release_seat(train_car_id: int, allocation_id: str, seat_number: str) -> bool:
    """POST the release payload to free a previously held seat. True on success."""
    payload = {
        "trainCarId": train_car_id,
        "allocationId": allocation_id,
        "seatNumber": seat_number,
    }
    try:
        post_tcdd_json(TCDD_RELEASE_SEAT_URL, payload)
        return True
    except TcddAuthError:
        return False
    except requests.exceptions.HTTPError as e:
        body = ""
        status: int | str = "?"
        if e.response is not None:
            status = e.response.status_code
            try:
                body = e.response.text[:300]
            except Exception:
                pass
        print(
            f"[tcdd] release-seat rejected wagon={train_car_id} seat={seat_number} "
            f"alloc={allocation_id}: HTTP {status} {body}",
            file=sys.stderr,
        )
        return False
    except Exception as e:
        print(f"[tcdd] release-seat error: {e}", file=sys.stderr)
        return False


def try_hold_seat(from_id: int, to_id: int, train_id: int, cabin_class_id: int) -> dict | None:
    """Load seat maps → pick a free seat in cabin_class_id → lock it.

    Returns {"seat", "wagon_index", "train_car_id", "allocation_id"} on success.
    train_car_id + allocation_id are what release_seat() needs later.
    """
    seat_maps = load_seat_maps(from_id, to_id, train_id)
    if not seat_maps:
        return None
    pick = pick_free_seat(seat_maps, cabin_class_id)
    if not pick:
        print(
            f"[tcdd] no free seat in cabin class {cabin_class_id} "
            f"for train {train_id}",
            file=sys.stderr,
        )
        return None
    response = select_seat(
        pick["train_car_id"], from_id, to_id, pick["seat_number"],
    )
    if response is None:
        return None
    allocation_id = response.get("allocationId")
    if not allocation_id:
        # Hold succeeded but we can't release it later — surface the warning so
        # the operator can investigate the response shape rather than silently
        # losing the release path.
        print(
            f"[tcdd] select-seat returned no allocationId "
            f"wagon={pick['train_car_id']} seat={pick['seat_number']}",
            file=sys.stderr,
        )
    return {
        "seat": pick["seat_number"],
        "wagon_index": pick["train_car_index"],
        "train_car_id": pick["train_car_id"],
        "allocation_id": allocation_id,
    }
