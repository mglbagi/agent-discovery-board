"""Postgres storage for listings. A separate database from the verification
service's — see README "Independence from the verification service."

Connection handling mirrors the verification service's app/core/db.py: a small
reused pool (Neon suspends idle compute and drops connections, so pooled
connections are health-checked on checkout and replaced transparently if dead),
autocommit (every query here is a single statement), server-side prepared
statements disabled (unsafe through Neon's PgBouncer "-pooler" endpoints), and
the schema created once at startup rather than per request.
"""

import os
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.core.activity import ACTIVITY_SQL
from app.core.constants import DUPLICATE_GUARDED_LISTING_TYPE
from app.core.endpoint import normalize_endpoint_url

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Set it to a Postgres connection string "
        "before starting the server."
    )

_POOL_MAX_SIZE = max(1, int(os.getenv("DB_POOL_MAX_SIZE", "5")))
_POOL_TIMEOUT_SECONDS = float(os.getenv("DB_POOL_TIMEOUT_SECONDS", "10"))

_pool: ConnectionPool | None = None
_schema_ready = False
_state_lock = threading.Lock()

# Raised by create/update when the partial unique index (one ACTIVE offering per
# normalized endpoint_url + submitter) rejects a write; the route maps it to 409.
UniqueViolation = psycopg.errors.UniqueViolation


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _state_lock:
            if _pool is None:
                pool = ConnectionPool(
                    DATABASE_URL,
                    min_size=1,
                    max_size=_POOL_MAX_SIZE,
                    timeout=_POOL_TIMEOUT_SECONDS,
                    kwargs={"prepare_threshold": None, "autocommit": True, "row_factory": dict_row},
                    check=ConnectionPool.check_connection,
                    open=False,
                )
                try:
                    pool.open(wait=True, timeout=_POOL_TIMEOUT_SECONDS)
                except BaseException:
                    pool.close()
                    raise
                _pool = pool
    return _pool


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _state_lock:
        if _schema_ready:
            return
    pool = _get_pool()
    with _state_lock:
        if _schema_ready:
            return
        with pool.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS listings (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    listing_type TEXT NOT NULL,
                    task_categories TEXT[] NOT NULL,
                    endpoint_url TEXT NOT NULL,
                    payment_wallet TEXT NOT NULL,
                    pricing_model TEXT,
                    pricing_amount TEXT,
                    erc8004_identity TEXT,
                    verification_agent_id TEXT,
                    submitted_by TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            # Additive migrations for tables created by earlier versions.
            conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ")
            conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS payment_options JSONB NOT NULL DEFAULT '[]'::jsonb")
            conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS endpoint_key TEXT")
            # Decided once at creation from the `test-` name prefix (app/core/demo_data.py); existing
            # rows are real (FALSE), whatever they are named.
            conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE")
            for row in conn.execute("SELECT id, endpoint_url FROM listings WHERE endpoint_key IS NULL").fetchall():
                conn.execute(
                    "UPDATE listings SET endpoint_key = %s WHERE id = %s",
                    (normalize_endpoint_url(row["endpoint_url"]), row["id"]),
                )
            # Every read filters on one or more of these; an externally reachable,
            # unauthenticated GET /listings must not be a sequential scan.
            for stmt in (
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS listings_submitted_by_idx ON listings (submitted_by)",
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS listings_status_idx ON listings (status)",
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS listings_listing_type_idx ON listings (listing_type)",
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS listings_task_categories_gin_idx "
                "ON listings USING GIN (task_categories)",
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS listings_activity_idx "
                f"ON listings (({ACTIVITY_SQL}) DESC, id DESC)",
            ):
                conn.execute(stmt)
            # Atomic duplicate guard: at most one ACTIVE listing of the guarded type
            # (offering) per (normalized endpoint, submitter). Announcements, notices and
            # requests are exempt. Not CONCURRENTLY, so a pre-existing duplicate makes
            # this fail cleanly instead of leaving an invalid index behind.
            # The guard is scoped by is_test: test listings only collide with other test
            # listings, never with real ones (so a demo can show a 409 without touching real data).
            for old_index in ("listings_active_endpoint_owner_uniq", "listings_active_offering_endpoint_owner_uniq"):
                conn.execute(f"DROP INDEX IF EXISTS {old_index}")  # earlier versions of this guard
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS listings_active_offering_scope_endpoint_owner_uniq "
                "ON listings (is_test, endpoint_key, lower(submitted_by)) "
                f"WHERE status = 'active' AND listing_type = '{DUPLICATE_GUARDED_LISTING_TYPE}'"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS listings_test_created_idx ON listings (created_at) WHERE is_test"
            )
        _schema_ready = True


def init_db() -> None:
    """Open the pool and create the schema. Called from app startup."""
    _ensure_schema()


def close_db() -> None:
    global _pool, _schema_ready
    with _state_lock:
        pool, _pool, _schema_ready = _pool, None, False
    if pool is not None:
        pool.close()


@contextmanager
def _connection() -> Iterator[psycopg.Connection]:
    _ensure_schema()
    with _get_pool().connection() as conn:
        yield conn


# Columns returned to callers (endpoint_key is internal).
_COLUMNS = (
    "id", "name", "description", "listing_type", "task_categories", "endpoint_url",
    "payment_wallet", "pricing_model", "pricing_amount", "payment_options", "erc8004_identity",
    "verification_agent_id", "submitted_by", "status", "is_test", "created_at", "updated_at", "last_seen_at",
)
_READ = ", ".join(_COLUMNS)
_WRITE_COLUMNS = _COLUMNS[:-1] + ("endpoint_key",)  # last_seen_at is only ever set by heartbeat


def _prepare(params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params)
    if "payment_options" in out:
        out["payment_options"] = Jsonb(out["payment_options"] or [])
    return out


def create_listing(row: dict[str, Any]) -> dict[str, Any]:
    row = {**row, "endpoint_key": normalize_endpoint_url(row["endpoint_url"])}
    placeholders = ", ".join(f"%({c})s" for c in _WRITE_COLUMNS)
    with _connection() as conn:
        return conn.execute(
            f"INSERT INTO listings ({', '.join(_WRITE_COLUMNS)}) VALUES ({placeholders}) RETURNING {_READ}",
            _prepare(row),
        ).fetchone()


def get_listing(listing_id: str) -> dict[str, Any] | None:
    with _connection() as conn:
        return conn.execute(f"SELECT {_READ} FROM listings WHERE id = %s", (listing_id,)).fetchone()


def find_active_duplicate(
    endpoint_url: str, submitted_by: str, exclude_id: str | None = None, is_test: bool = False
) -> str | None:
    """id of the ACTIVE offering (the only guarded type) with the same normalized endpoint
    and submitter, if any. Test listings and real listings are separate scopes."""
    with _connection() as conn:
        row = conn.execute(
            "SELECT id FROM listings WHERE endpoint_key = %s AND lower(submitted_by) = lower(%s) "
            "AND status = 'active' AND listing_type = %s AND is_test = %s AND (%s::text IS NULL OR id <> %s) LIMIT 1",
            (
                normalize_endpoint_url(endpoint_url),
                submitted_by,
                DUPLICATE_GUARDED_LISTING_TYPE,
                is_test,
                exclude_id,
                exclude_id,
            ),
        ).fetchone()
    return row["id"] if row else None


def update_listing(listing_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    """`patch` may contain any subset of the mutable columns (never id/submitted_by/
    created_at — the route layer never passes those). Always bumps updated_at, which
    the caller supplies alongside the rest of `patch`. A changed endpoint_url gets its
    endpoint_key recomputed here so the two can never drift."""
    if not patch:
        raise ValueError("patch must not be empty")
    patch = dict(patch)
    if "endpoint_url" in patch:
        patch["endpoint_key"] = normalize_endpoint_url(patch["endpoint_url"])
    set_clause = ", ".join(f"{col} = %({col})s" for col in patch)
    with _connection() as conn:
        return conn.execute(
            f"UPDATE listings SET {set_clause} WHERE id = %(id)s RETURNING {_READ}",
            {**_prepare(patch), "id": listing_id},
        ).fetchone()


def set_listing_status(listing_id: str, status: str, now: datetime) -> dict[str, Any] | None:
    return update_listing(listing_id, {"status": status, "updated_at": now})


def record_heartbeat(listing_id: str, now: datetime, cutoff: datetime) -> dict[str, Any] | None:
    """Set last_seen_at = now, but only if the previous heartbeat is at or before
    `cutoff` (or there was none). Conditional in SQL, so two concurrent heartbeats
    cannot both succeed inside one window. Returns None when nothing was updated."""
    with _connection() as conn:
        return conn.execute(
            f"UPDATE listings SET last_seen_at = %(now)s WHERE id = %(id)s AND status = 'active' "
            f"AND (last_seen_at IS NULL OR last_seen_at <= %(cutoff)s) RETURNING {_READ}",
            {"now": now, "id": listing_id, "cutoff": cutoff},
        ).fetchone()


def list_listings(
    *,
    listing_type: str | None,
    task_categories: list[str] | None,
    q: str | None,
    status: str | None,
    limit: int,
    offset: int,
    cursor: tuple[datetime, str] | None = None,
    include_test: bool = False,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Returns (page of rows, total matching the filters, whether more rows follow).
    Order is newest last activity first, id as the tiebreaker - a total order, so a
    cursor (last_activity_at, id) is a stable resume point."""
    conditions: list[str] = []
    params: dict[str, Any] = {}

    conditions.append("status = %(status)s")
    params["status"] = status if status is not None else "active"
    if not include_test:
        conditions.append("NOT is_test")

    if listing_type is not None:
        conditions.append("listing_type = %(listing_type)s")
        params["listing_type"] = listing_type

    if task_categories:
        conditions.append("task_categories && %(task_categories)s::text[]")
        params["task_categories"] = task_categories

    if q:
        # Explicit ESCAPE clause: a caller typing a literal '%' or '_' searches for
        # that literal character rather than it acting as a LIKE wildcard.
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append("(name ILIKE %(q)s ESCAPE '\\' OR description ILIKE %(q)s ESCAPE '\\')")
        params["q"] = f"%{escaped}%"

    total_where = " AND ".join(conditions)

    page_conditions = list(conditions)
    page_params = dict(params)
    if cursor is not None:
        page_conditions.append(f"(({ACTIVITY_SQL}), id) < (%(cursor_activity)s, %(cursor_id)s)")
        page_params["cursor_activity"], page_params["cursor_id"] = cursor
    page_where = " AND ".join(page_conditions)

    with _connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM listings WHERE {total_where}", params).fetchone()["n"]
        rows = conn.execute(
            f"SELECT {_READ} FROM listings WHERE {page_where} "
            f"ORDER BY ({ACTIVITY_SQL}) DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s",
            {**page_params, "limit": limit + 1, "offset": offset},
        ).fetchall()
    return rows[:limit], total, len(rows) > limit


def purge_expired_test_listings(cutoff: datetime, limit: int) -> list[str]:
    """Delete up to `limit` test listings created before `cutoff` (oldest first); returns
    their ids. One indexed statement (listings_test_created_idx); only is_test rows can
    ever match, so a real listing cannot be removed by this."""
    with _connection() as conn:
        rows = conn.execute(
            "DELETE FROM listings WHERE id IN ("
            "SELECT id FROM listings WHERE is_test AND created_at < %(cutoff)s ORDER BY created_at LIMIT %(limit)s"
            ") RETURNING id",
            {"cutoff": cutoff, "limit": limit},
        ).fetchall()
    return [r["id"] for r in rows]
