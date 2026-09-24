"""Listing freshness, computed from data the board already holds - no outbound calls.

`last_activity_at` is the latest of created_at, updated_at and last_seen_at (the
heartbeat). A listing is `stale` when that is older than STALE_AFTER_DAYS. The same
expression is used in SQL for sorting (app/core/db.py, ACTIVITY_SQL) and in Python for
the response fields and cursors, and must stay identical.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any


def _stale_after_days() -> float:
    raw = os.getenv("STALE_AFTER_DAYS", "60")
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"STALE_AFTER_DAYS must be a number of days, got {raw!r}") from exc
    if not 0 < value <= 3650:
        raise RuntimeError("STALE_AFTER_DAYS must be greater than 0 and at most 3650")
    return value


STALE_AFTER_DAYS = _stale_after_days()

# Once-per-window limit for POST /listings/{id}/heartbeat.
HEARTBEAT_MIN_INTERVAL = timedelta(hours=24)

# Keep in step with last_activity_at() below.
ACTIVITY_SQL = "GREATEST(created_at, updated_at, COALESCE(last_seen_at, created_at))"


def last_activity_at(row: dict[str, Any]) -> datetime:
    return max(row["created_at"], row["updated_at"], row.get("last_seen_at") or row["created_at"])


def is_stale(row: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return now - last_activity_at(row) > timedelta(days=STALE_AFTER_DAYS)
