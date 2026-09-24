"""`test-` listings: temporary demo/smoke-test data that never pollutes real results.

Same idea as the verification service's reserved `test-` agent_ids, adapted to a board
whose demos need listings to actually persist (heartbeat, duplicates and cursors all act
on stored rows):

  * A listing whose `name` starts with exactly `test-` is a TEST listing. It is decided
    once, at creation, and stored (`is_test`); the prefix cannot be added to or removed
    from an existing listing, so a real listing can never be silently turned into
    something that gets purged.
  * Test listings work normally by id (GET, PATCH, heartbeat, DELETE) but are hidden from
    default browse, search and the search_listings MCP tool (opt in with include_test),
    never trigger or block duplicate detection for real listings (they only collide with
    other test listings, so a demo can still show a 409), and never cost a badge lookup.
  * They are purged TEST_LISTING_TTL_HOURS after creation (default 24). The board runs on
    a free tier that sleeps when idle, so there is no background timer: the purge runs at
    startup and, throttled, during normal requests (app/core/maintenance.py), and the
    operator script has --purge-test as a fallback.
"""

import os
from datetime import datetime, timedelta

TEST_NAME_PREFIX = "test-"


def is_test_name(name: str) -> bool:
    return name.startswith(TEST_NAME_PREFIX)


def _ttl_hours() -> float:
    raw = os.getenv("TEST_LISTING_TTL_HOURS", "24")
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"TEST_LISTING_TTL_HOURS must be a number of hours, got {raw!r}") from exc
    if not 0 < value <= 720:
        raise RuntimeError("TEST_LISTING_TTL_HOURS must be greater than 0 and at most 720")
    return value


TEST_LISTING_TTL_HOURS = _ttl_hours()

# At most this many rows are removed per purge run, so a purge is always one cheap,
# indexed statement; a backlog simply drains over the next runs.
PURGE_BATCH_SIZE = 200


def _purge_interval_seconds() -> float:
    raw = os.getenv("TEST_PURGE_MIN_INTERVAL_SECONDS", "300")
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"TEST_PURGE_MIN_INTERVAL_SECONDS must be a number of seconds, got {raw!r}") from exc
    if not 1 <= value <= 86_400:
        raise RuntimeError("TEST_PURGE_MIN_INTERVAL_SECONDS must be between 1 and 86400")
    return value


PURGE_MIN_INTERVAL_SECONDS = _purge_interval_seconds()


def ttl() -> timedelta:
    return timedelta(hours=TEST_LISTING_TTL_HOURS)


def expires_at(created_at: datetime) -> datetime:
    return created_at + ttl()
