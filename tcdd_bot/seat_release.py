"""Release a previously held seat back to TCDD inventory.

Lives outside seat_hold.py because release is a user-triggered action (the
Telegram Release button), not part of the load → pick → lock orchestration.
"""
from __future__ import annotations

import sys

import requests

from tcdd_bot.config import TCDD_RELEASE_SEAT_URL
from tcdd_bot.tcdd_api import TcddAuthError, post_tcdd_json


def release_seat(train_car_id: int, allocation_id: str, seat_number: str) -> bool:
    """POST the release payload to free a previously held seat. True on success.

    TCDD answers a successful release with an empty 2xx body — no JSON. The
    post_tcdd_json helper normalises that to {}, so any non-exception return
    here means TCDD accepted the release.
    """
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
