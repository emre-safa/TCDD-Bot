"""Centralized configuration: env vars, URLs, intervals, paths.

Importing this module performs the env-var checks (fail fast).
"""
from __future__ import annotations

import os
from datetime import timedelta, timezone
from pathlib import Path


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TCDD_AUTH = os.environ.get("TCDD_AUTH_TOKEN", "").strip()

if not BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN environment variable is not set")
if not TCDD_AUTH:
    raise SystemExit("TCDD_AUTH_TOKEN environment variable is not set")

CHECK_INTERVAL_SECONDS = 60
TELEGRAM_LONG_POLL_TIMEOUT = 25
PER_API_CALL_DELAY = 1.5  # be polite to the TCDD endpoint

# Data files sit alongside the launcher (one directory up from this package).
BASE_DIR = Path(__file__).resolve().parent.parent
SUBS_FILE = BASE_DIR / "subscriptions.json"
STATIONS_FILE = BASE_DIR / "stations.json"

TCDD_URL = (
    "https://web-api-prod-ytp.tcddtasimacilik.gov.tr"
    "/tms/train/train-availability?environment=dev&userId=1"
)
TCDD_LOAD_BY_TRAIN_URL = (
    "https://web-api-prod-ytp.tcddtasimacilik.gov.tr"
    "/tms/seat-maps/load-by-train-id?environment=dev&userId=1"
)
TCDD_SELECT_SEAT_URL = (
    "https://web-api-prod-ytp.tcddtasimacilik.gov.tr"
    "/tms/inventory/select-seat?environment=dev&userId=1"
)
TCDD_RELEASE_SEAT_URL = (
    "https://web-api-prod-ytp.tcddtasimacilik.gov.tr"
    "/tms/inventory/release-seat?environment=dev&userId=1"
)
TARGET_CABIN_CLASS = "EKONOMİ"

# Single-passenger hold for now; can become per-watch / per-user later.
BOOKING_GENDER = "M"

# All TCDD timestamps in segments[].departureTime / arrivalTime are UTC epoch ms.
# Display them in Türkiye time (GMT+03:00, no DST since 2016).
DISPLAY_TZ = timezone(timedelta(hours=3))

TCDD_HTTP_TIMEOUT = 30           # seconds per attempt
TCDD_HTTP_RETRIES = 1            # extra attempts after the first on read timeout
TCDD_HTTP_RETRY_DELAY = 1.0      # seconds between attempts
