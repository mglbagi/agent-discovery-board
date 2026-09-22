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
from psycopg_pool import ConnectionPool

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
            # Every read filters on one or more of these; an externally reachable,
            # unauthenticated GET /listings must not be a sequential scan.
            for stmt in (
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS listings_submitted_by_idx ON listings (submitted_by)",
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS listings_status_idx ON listings (status)",
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS listings_listing_type_idx ON listings (listing_type)",
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS listings_task_categories_gin_idx "
                "ON listings USING GIN (task_categories)",
            ):
                conn.execute(stmt)
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


_COLUMNS = (
    "id", "name", "description", "listing_type", "task_categories", "endpoint_url",
    "payment_wallet", "pricing_model", "pricing_amount", "erc8004_identity",
    "verification_agent_id", "submitted_by", "status", "created_at", "updated_at",
)


def create_listing(row: dict[str, Any]) -> dict[str, Any]:
    cols = _COLUMNS
    placeholders = ", ".join(f"%({c})s" for c in cols)
    with _connection() as conn:
        return conn.execute(
            f"INSERT INTO listings ({', '.join(cols)}) VALUES ({placeholders}) "
            f"RETURNING {', '.join(cols)}",
            row,
        ).fetchone()


def get_listing(listing_id: str) -> dict[str, Any] | None:
    with _connection() as conn:
        return conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM listings WHERE id = %s", (listing_id,)
        ).fetchone()


def update_listing(listing_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    """`patch` may contain any subset of the mutable columns (never id/submitted_by/
    created_at — the route layer never passes those). Always bumps updated_at, which
    the caller supplies alongside the rest of `patch`."""
    if not patch:
        raise ValueError("patch must not be empty")
    set_clause = ", ".join(f"{col} = %({col})s" for col in patch)
    with _connection() as conn:
        return conn.execute(
            f"UPDATE listings SET {set_clause} WHERE id = %(id)s RETURNING {', '.join(_COLUMNS)}",
            {**patch, "id": listing_id},
        ).fetchone()


def set_listing_status(listing_id: str, status: str, now: datetime) -> dict[str, Any] | None:
    return update_listing(listing_id, {"status": status, "updated_at": now})


def list_listings(
    *,
    listing_type: str | None,
    task_categories: list[str] | None,
    q: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    conditions: list[str] = []
    params: dict[str, Any] = {}

    conditions.append("status = %(status)s")
    params["status"] = status if status is not None else "active"

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

    where = " AND ".join(conditions)
    with _connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM listings WHERE {where}", params).fetchone()["n"]
        rows = conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM listings WHERE {where} "
            "ORDER BY created_at DESC LIMIT %(limit)s OFFSET %(offset)s",
            {**params, "limit": limit, "offset": offset},
        ).fetchall()
    return rows, total
