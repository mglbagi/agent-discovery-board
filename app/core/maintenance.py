"""Housekeeping that must not depend on a timer (the free tier sleeps when idle):
startup, plus a throttled opportunistic purge of expired `test-` listings that piggybacks
on normal requests. Everything here is bounded and best-effort - a failed purge is logged
and never fails the request that happened to trigger it."""

import logging
import threading
import time
from datetime import datetime, timezone

from app.core import db, demo_data

logger = logging.getLogger("app.maintenance")

_lock = threading.Lock()
_last_run: float | None = None  # time.monotonic() of the last purge attempt


def purge_expired_test_listings() -> list[str]:
    """Delete (at most PURGE_BATCH_SIZE) test listings older than the TTL; returns their ids."""
    cutoff = datetime.now(timezone.utc) - demo_data.ttl()
    return db.purge_expired_test_listings(cutoff, demo_data.PURGE_BATCH_SIZE)


def maybe_purge() -> None:
    """Run the purge unless one already ran within PURGE_MIN_INTERVAL_SECONDS. Cheap when
    throttled (a lock and a clock read); when it does run it is one indexed statement."""
    global _last_run
    now = time.monotonic()
    with _lock:
        if _last_run is not None and now - _last_run < demo_data.PURGE_MIN_INTERVAL_SECONDS:
            return
        _last_run = now
    try:
        removed = purge_expired_test_listings()
        if removed:
            logger.info("purged %d expired test listing(s)", len(removed))
    except Exception:  # noqa: BLE001 - housekeeping must never break a request
        logger.exception("opportunistic test-listing purge failed")


def startup_maintenance() -> None:
    """Open the database, create/migrate the schema, and purge expired test listings.
    Best-effort: if the database is briefly unreachable the service still starts and the
    first request retries the same initialization (db._ensure_schema is lazy)."""
    global _last_run
    try:
        db.init_db()
    except Exception:  # noqa: BLE001
        logging.getLogger("app.db").exception("Database init at startup failed; will retry on first use")
        return
    try:
        removed = purge_expired_test_listings()
        with _lock:
            _last_run = time.monotonic()
        if removed:
            logger.info("startup purge removed %d expired test listing(s)", len(removed))
    except Exception:  # noqa: BLE001
        logger.exception("startup test-listing purge failed")
