"""Postgres storage for listings. A separate database from the verification
service's — see README "Independence from the verification service."

Connection handling mirrors the verification service's app/core/db.py: a small
reused pool (Neon suspends idle compute and drops connections, so pooled
connections are health-checked on checkout and replaced transparently if dead),
autocommit (every query here is a single statement), server-side prepared
statements disabled (unsafe through Neon's PgBouncer "-pooler" endpoints), and
the schema created once at startup rather than per request.
"""

import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.core.activity import ACTIVITY_SQL, STALE_AFTER_DAYS
from app.core.constants import DUPLICATE_GUARDED_LISTING_TYPE
from app.core.endpoint import normalize_endpoint_url
from app.core.pagination import AnyCursor
from app.core.stablecoins import stablecoin_keys

logger = logging.getLogger("app.db")

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

# Below this many full-text hits, try the pg_trgm similarity fallback instead (typo/
# partial-word tolerance) - see list_listings. Only reached when full-text found
# nothing at all, so this is "zero" in practice, kept as a threshold rather than a
# literal 0 check in case that's ever worth loosening.
FULLTEXT_FALLBACK_THRESHOLD = 0
# word_similarity() scores (see list_listings): empirically, real typos ("verfy" for
# "verify", "recipt" for "receipt") against a realistic listing description score
# ~0.5; unrelated/noise queries score under 0.15. 0.3 sits well inside that gap.
TRIGRAM_SIMILARITY_THRESHOLD = 0.3

# Set during _ensure_schema(): whether pg_trgm (and its GIN index on listings) are
# actually usable on this database. Best-effort, not a required dependency - if an
# environment can't install the extension, full-text search still works fully; only
# the typo-tolerant fallback is unavailable. See _ensure_schema().
_trigram_available = False


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


_SEARCH_VECTOR_EXPR = (
    "setweight(to_tsvector('english', coalesce({name}, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce({description}, '')), 'B') || "
    "setweight(to_tsvector('english', array_to_string(coalesce({task_categories}, ARRAY[]::text[]), ' ')), 'C')"
)


def _ensure_schema() -> None:
    global _schema_ready, _trigram_available
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

            # --- Full-text search (app/core/db.py's list_listings) -------------------------
            # search_vector can't be a GENERATED column itself: Postgres's array-to-text
            # functions/casts (needed to fold task_categories in) aren't IMMUTABLE on this
            # version, which GENERATED STORED requires - confirmed empirically, not just
            # read from docs. A BEFORE INSERT/UPDATE trigger has no such restriction and
            # keeps it exactly as current as a generated column would.
            conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS search_vector tsvector")
            # trigram_text has no arrays in it, so it CAN be a plain generated column.
            conn.execute(
                "ALTER TABLE listings ADD COLUMN IF NOT EXISTS trigram_text text "
                "GENERATED ALWAYS AS (coalesce(name, '') || ' ' || coalesce(description, '')) STORED"
            )
            conn.execute(
                f"""
                CREATE OR REPLACE FUNCTION listings_search_vector_update() RETURNS trigger AS $$
                BEGIN
                    NEW.search_vector := {_SEARCH_VECTOR_EXPR.format(
                        name="NEW.name", description="NEW.description", task_categories="NEW.task_categories"
                    )};
                    RETURN NEW;
                END
                $$ LANGUAGE plpgsql
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS listings_search_vector_trg ON listings")
            conn.execute(
                "CREATE TRIGGER listings_search_vector_trg BEFORE INSERT OR UPDATE ON listings "
                "FOR EACH ROW EXECUTE FUNCTION listings_search_vector_update()"
            )
            # One-time backfill for rows written before this migration (the trigger only
            # covers inserts/updates from here on); idempotent via the IS NULL filter, so
            # it's a no-op on every startup after the first.
            conn.execute(
                f"UPDATE listings SET search_vector = "
                f"{_SEARCH_VECTOR_EXPR.format(name='name', description='description', task_categories='task_categories')} "
                "WHERE search_vector IS NULL"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS listings_search_vector_gin_idx ON listings USING GIN (search_vector)")

            # pg_trgm is an optional enhancement (typo tolerance when full-text finds
            # nothing), not a hard dependency: if an environment won't allow the extension,
            # full-text search still works completely - only that one fallback is skipped.
            try:
                conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS listings_trigram_gin_idx ON listings USING GIN (trigram_text gin_trgm_ops)"
                )
                _trigram_available = True
            except Exception:  # noqa: BLE001
                logger.warning("pg_trgm unavailable; the typo-tolerant search fallback is disabled", exc_info=True)
                _trigram_available = False

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS search_log (
                    id SERIAL PRIMARY KEY,
                    query TEXT NOT NULL,
                    result_count INTEGER NOT NULL,
                    searched_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS search_log_searched_at_idx ON search_log (searched_at)")

            # --- Imported listings (app/core/imports.py) ------------------------------------
            # claimed defaults TRUE: every listing created the normal way (POST /listings) is
            # fully owned by its submitted_by from the start. Only the import path explicitly
            # sets it FALSE; existing rows (all organic) are correctly TRUE under this default.
            conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS claimed BOOLEAN NOT NULL DEFAULT TRUE")
            conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS source TEXT")
            conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS source_url TEXT")
            conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS imported_at TIMESTAMPTZ")
            conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ")
            # Set by a sync run that no longer finds this listing at its source; cleared again
            # if a later sync finds it again. Feeds is_stale() (app/core/activity.py) directly,
            # independent of the normal activity-age threshold.
            conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS missing_from_source_since TIMESTAMPTZ")
            # One row per imported listing per source: re-syncing upserts by this pair rather
            # than creating duplicates (app/core/db.py's import_upsert).
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS listings_source_endpoint_uniq "
                "ON listings (source, endpoint_key) WHERE source IS NOT NULL"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS do_not_import (
                    source TEXT NOT NULL,
                    endpoint_key TEXT NOT NULL,
                    reason TEXT,
                    created_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (source, endpoint_key)
                )
                """
            )

            # --- Agent navigation: structured filters, facets, templates ---------------------
            # A JSON Schema for this service's output, if it has one (has_template filter and
            # a listing's verify_output next_action - see app/api/routes/listings.py).
            conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS output_schema JSONB")
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


# Columns returned to callers (endpoint_key and missing_from_source_since are internal -
# the latter only ever feeds is_stale(), never shown as its own response field).
_COLUMNS = (
    "id", "name", "description", "listing_type", "task_categories", "endpoint_url",
    "payment_wallet", "pricing_model", "pricing_amount", "payment_options", "erc8004_identity",
    "verification_agent_id", "submitted_by", "status", "is_test", "created_at", "updated_at", "last_seen_at",
    "claimed", "source", "source_url", "imported_at", "last_synced_at", "missing_from_source_since",
    "output_schema",
)
# last_seen_at is only ever set by heartbeat; missing_from_source_since only by a sync noticing
# a listing is gone. Every create_listing() caller supplies the rest, including the import path.
_WRITE_COLUMNS = tuple(c for c in _COLUMNS if c not in ("last_seen_at", "missing_from_source_since")) + ("endpoint_key",)
_READ = ", ".join(_COLUMNS)


def _prepare(params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params)
    if "payment_options" in out:
        out["payment_options"] = Jsonb(out["payment_options"] or [])
    if "output_schema" in out and out["output_schema"] is not None:
        out["output_schema"] = Jsonb(out["output_schema"])
    return out


# Defaults for columns a caller may not know or care about - an organic listing (the
# overwhelming common case) is claimed from the start with no import metadata at all.
# The import path (app/core/imports.py's build_import_row) always sets these explicitly.
_CREATE_DEFAULTS = {
    "claimed": True, "source": None, "source_url": None, "imported_at": None, "last_synced_at": None,
    "output_schema": None,
}


def create_listing(row: dict[str, Any]) -> dict[str, Any]:
    row = {**_CREATE_DEFAULTS, **row, "endpoint_key": normalize_endpoint_url(row["endpoint_url"])}
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


def _filter_conditions(
    status: str | None,
    include_test: bool,
    listing_type: str | None,
    task_categories: list[str] | None,
    claimed: bool | None = None,
    payment_network: str | None = None,
    max_price: float | None = None,
    has_template: bool | None = None,
    stale: bool | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """The filters shared by every search mode below (status/include_test/listing_type/
    task_categories/claimed/payment_network/max_price/has_template/stale) - everything
    except q itself, which each mode applies differently."""
    conditions = ["status = %(status)s"]
    params: dict[str, Any] = {"status": status if status is not None else "active"}
    if not include_test:
        conditions.append("NOT is_test")
    if listing_type is not None:
        conditions.append("listing_type = %(listing_type)s")
        params["listing_type"] = listing_type
    if task_categories:
        conditions.append("task_categories && %(task_categories)s::text[]")
        params["task_categories"] = task_categories
    if claimed is not None:
        conditions.append("claimed = %(claimed)s")
        params["claimed"] = claimed
    if payment_network is not None or max_price is not None:
        # One payment_option must satisfy BOTH the network and the price cap together,
        # not either independently - "pay for this on Base for under $0.05" means one
        # entry matching both, not any entry matching one.
        #
        # max_price is USD. A payment_option's `amount` is a decimal string in that
        # ASSET's own whole-token units (PaymentOption's own docs: "0.02" means 0.02 of
        # the asset, not an atomic/smallest unit) - directly comparable to a USD figure
        # only for a recognized USD stablecoin (~1 token = $1; app/core/stablecoins.py),
        # since this board has no price oracle for anything else. A max_price filter
        # therefore only ever matches stablecoin payment_options; a listing priced only
        # in ETH, SOL or some other token is excluded from it entirely, not guessed at.
        conditions.append(
            "EXISTS (SELECT 1 FROM jsonb_array_elements(payment_options) AS po WHERE "
            "(%(payment_network)s::text IS NULL OR po->>'network' = %(payment_network)s) AND "
            "(%(max_price)s::numeric IS NULL OR ("
            "(po->>'network' || '|' || lower(po->>'asset')) = ANY(%(stablecoin_keys)s) AND "
            "(po->>'amount')::numeric <= %(max_price)s)))"
        )
        params["payment_network"] = payment_network
        params["max_price"] = max_price
        params["stablecoin_keys"] = stablecoin_keys()
    if has_template is not None:
        conditions.append("output_schema IS NOT NULL" if has_template else "output_schema IS NULL")
    if stale is not None:
        # Mirrors app/core/activity.py's is_stale() exactly: missing from an import sync,
        # or no activity for longer than the configured threshold.
        stale_expr = (
            f"(missing_from_source_since IS NOT NULL OR "
            f"now() - ({ACTIVITY_SQL}) > %(stale_days)s * interval '1 day')"
        )
        conditions.append(stale_expr if stale else f"NOT {stale_expr}")
        params["stale_days"] = STALE_AFTER_DAYS
    return conditions, params


def _log_search_query(conn: psycopg.Connection, query: str, result_count: int) -> None:
    """Query text, result count and timestamp only - no IP, no other request data.
    Best-effort: a logging failure must never break a search."""
    try:
        conn.execute(
            "INSERT INTO search_log (query, result_count, searched_at) VALUES (%s, %s, %s)",
            (query, result_count, datetime.now(timezone.utc)),
        )
    except Exception:  # noqa: BLE001
        logger.warning("failed to log a search query", exc_info=True)


def _paged(
    conn: psycopg.Connection,
    *,
    where: str,
    params: dict[str, Any],
    order_expr: str,
    cursor_expr: str | None,
    cursor_params: dict[str, Any],
    limit: int,
    offset: int,
    cursor_value_expr: str | None,
) -> list[dict[str, Any]]:
    """One page of listings rows, ordered by `order_expr` DESC, id DESC, each carrying an
    extra `_cursor_value` column (from `cursor_value_expr`) when one is given - the raw
    value the caller needs to build the next page's cursor, popped off before a row is
    ever shown to an API caller. Fetches limit+1 rows so has_more can be read off the
    length without a second query."""
    conditions = where
    all_params = dict(params)
    if cursor_expr is not None:
        conditions = f"{where} AND {cursor_expr}"
        all_params.update(cursor_params)
    select_extra = f", {cursor_value_expr} AS _cursor_value" if cursor_value_expr else ""
    return conn.execute(
        f"SELECT {_READ}{select_extra} FROM listings WHERE {conditions} "
        f"ORDER BY {order_expr} DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s",
        {**all_params, "limit": limit + 1, "offset": offset},
    ).fetchall()


def list_listings(
    *,
    listing_type: str | None,
    task_categories: list[str] | None,
    q: str | None,
    status: str | None,
    limit: int,
    offset: int,
    cursor: AnyCursor | None = None,
    include_test: bool = False,
    claimed: bool | None = None,
    payment_network: str | None = None,
    max_price: float | None = None,
    has_template: bool | None = None,
    stale: bool | None = None,
) -> tuple[list[dict[str, Any]], int, bool, str]:
    """Returns (page of rows, total matching the filters, whether more rows follow, mode).

    No `q`: newest last activity first, id as tiebreaker (unchanged from before search
    had ranking) - `cursor` here is an ActivityCursor, and `mode` is always "activity".

    With `q`: full-text search (websearch_to_tsquery against name/description/
    task_categories, weighted name > description > categories), ranked by ts_rank, id as
    tiebreaker - `cursor` is a RankCursor and `mode` is "rank". If that finds nothing at
    all, falls back to a pg_trgm similarity search (typo/partial-word tolerance) ordered
    by similarity, id as tiebreaker - `cursor` is a SimilarityCursor and `mode` is
    "similarity". Which of the two a multi-page search continues in is decided once, on
    the first page, and then driven by the cursor itself on every later page (see
    app/core/pagination.py), not re-decided per page.

    `mode` tells the caller which cursor-encoding function to use for the next page; every
    row also carries a transient `_cursor_value` key (the raw ordering value for that row -
    last_activity_at/rank/similarity, or absent when `mode` is "activity") that must be
    popped before a row is returned from the API - only the last row's value is actually
    needed, but it's cheap to leave on every row rather than special-casing.

    Every search this is given (every call with a non-empty q) is logged to search_log:
    the query text, the total result count, and when - nothing else.

    `claimed`: None (default) applies no filter; True/False restrict to claimed or
    unclaimed (imported, not yet claimed) listings respectively. `payment_network`/
    `max_price`: match a single payment_option satisfying both together when both are
    given. `has_template`: output_schema is/isn't set. `stale`: mirrors is_stale()
    exactly (app/core/activity.py).
    """
    conditions, params = _filter_conditions(
        status, include_test, listing_type, task_categories, claimed, payment_network, max_price, has_template, stale
    )
    where = " AND ".join(conditions)

    with _connection() as conn:
        if not q:
            cursor_expr, cursor_params = None, {}
            if cursor is not None:
                cursor_expr = f"(({ACTIVITY_SQL}), id) < (%(cursor_activity)s, %(cursor_id)s)"
                cursor_params = {"cursor_activity": cursor.activity, "cursor_id": cursor.id}
            total = conn.execute(f"SELECT COUNT(*) AS n FROM listings WHERE {where}", params).fetchone()["n"]
            rows = _paged(
                conn, where=where, params=params, order_expr=f"({ACTIVITY_SQL})",
                cursor_expr=cursor_expr, cursor_params=cursor_params, limit=limit, offset=offset,
                cursor_value_expr=None,
            )
            return rows[:limit], total, len(rows) > limit, "activity"

        # q given: decide full-text vs. trigram once (first page), then trust the cursor's
        # own mode for every later page of the same search.
        mode = cursor.mode if cursor is not None else None  # "rank" | "similarity" | None (first page)
        ft_conditions = conditions + ["search_vector @@ websearch_to_tsquery('english', %(q)s)"]
        ft_where = " AND ".join(ft_conditions)
        ft_params = {**params, "q": q}
        # The ::double precision cast matters for cursor correctness, not just style:
        # ts_rank returns `real` (float4), and Postgres's default text formatting for
        # `real` doesn't carry enough digits to round-trip exactly - a cursor value
        # fetched out, sent back as a parameter, and compared with `=` against a freshly
        # computed `real` can come back NOT equal, which silently breaks the keyset
        # predicate below (confirmed empirically: pagination never advanced). Casting to
        # double precision here makes the round trip exact.
        rank_expr = "ts_rank(search_vector, websearch_to_tsquery('english', %(q)s))::double precision"

        if mode is None:
            ft_total = conn.execute(f"SELECT COUNT(*) AS n FROM listings WHERE {ft_where}", ft_params).fetchone()["n"]
            mode = "rank" if ft_total > FULLTEXT_FALLBACK_THRESHOLD else "similarity"
        else:
            ft_total = None  # already committed to `mode` by the cursor; computed below only if needed

        if mode == "rank":
            total = ft_total if ft_total is not None else conn.execute(
                f"SELECT COUNT(*) AS n FROM listings WHERE {ft_where}", ft_params
            ).fetchone()["n"]
            cursor_expr, cursor_params = None, {}
            if cursor is not None:
                cursor_expr = f"({rank_expr}, id) < (%(cursor_rank)s, %(cursor_id)s)"
                cursor_params = {"cursor_rank": cursor.value, "cursor_id": cursor.id}
            rows = _paged(
                conn, where=ft_where, params=ft_params, order_expr=rank_expr,
                cursor_expr=cursor_expr, cursor_params=cursor_params, limit=limit, offset=offset,
                cursor_value_expr=rank_expr,
            )
        elif _trigram_available:
            # word_similarity(query, document), not similarity(): plain trigram
            # similarity divides by the UNION of trigrams in both strings, so a short
            # query against a long name+description dilutes to near zero no matter how
            # good a match it is (confirmed empirically: ~0.01 for a real typo against a
            # realistic listing description). word_similarity instead scores the best-
            # matching EXTENT of the document against the query, which is what "does
            # this listing contain something close to what they typed" actually means.
            # Cast to double precision for the same round-trip-exactness reason as
            # rank_expr above - word_similarity also returns `real`.
            sim_expr = "word_similarity(%(q)s, trigram_text)::double precision"
            # The `<%` operator (not a raw word_similarity(...) > threshold comparison)
            # is what lets the planner use the trigram GIN index here.
            # `%%` because this SQL text is itself a psycopg pyformat template: a bare
            # `%` (as in the `<%` operator) would be misread as a placeholder escape.
            trgm_conditions = conditions + ["%(q)s <%% trigram_text"]
            trgm_where = " AND ".join(trgm_conditions)
            cursor_expr, cursor_params = None, {}
            if cursor is not None:
                cursor_expr = f"({sim_expr}, id) < (%(cursor_sim)s, %(cursor_id)s)"
                cursor_params = {"cursor_sim": cursor.value, "cursor_id": cursor.id}
            with conn.transaction():
                # SET LOCAL takes no query parameters; the threshold is an internal
                # constant, never user input, so inlining it is safe. Scoped to this
                # transaction only - the pool's connections stay in autocommit between
                # requests.
                conn.execute(f"SET LOCAL pg_trgm.word_similarity_threshold = {TRIGRAM_SIMILARITY_THRESHOLD}")
                total = conn.execute(f"SELECT COUNT(*) AS n FROM listings WHERE {trgm_where}", ft_params).fetchone()["n"]
                rows = _paged(
                    conn, where=trgm_where, params=ft_params, order_expr=sim_expr,
                    cursor_expr=cursor_expr, cursor_params=cursor_params, limit=limit, offset=offset,
                    cursor_value_expr=sim_expr,
                )
        else:
            total, rows = 0, []  # trigram mode was chosen (full-text found nothing) but pg_trgm isn't available here

        _log_search_query(conn, q, total)
        return rows[:limit], total, len(rows) > limit, mode


def _facet_breakdowns(conn: psycopg.Connection, where: str, params: dict[str, Any]) -> dict[str, Any]:
    total = conn.execute(f"SELECT COUNT(*) AS n FROM listings WHERE {where}", params).fetchone()["n"]
    by_category = {
        r["c"]: r["n"]
        for r in conn.execute(
            f"SELECT c, COUNT(*) AS n FROM listings, unnest(task_categories) AS c WHERE {where} GROUP BY c", params
        ).fetchall()
    }
    by_type = {
        r["listing_type"]: r["n"]
        for r in conn.execute(f"SELECT listing_type, COUNT(*) AS n FROM listings WHERE {where} GROUP BY listing_type", params).fetchall()
    }
    by_network = {
        r["network"]: r["n"]
        for r in conn.execute(
            "SELECT po->>'network' AS network, COUNT(DISTINCT listings.id) AS n FROM listings, "
            f"jsonb_array_elements(payment_options) AS po WHERE {where} GROUP BY po->>'network'",
            params,
        ).fetchall()
    }
    by_source = {
        (r["source"] or "none"): r["n"]
        for r in conn.execute(f"SELECT source, COUNT(*) AS n FROM listings WHERE {where} GROUP BY source", params).fetchall()
    }
    return {"total": total, "by_task_category": by_category, "by_listing_type": by_type, "by_network": by_network, "by_source": by_source}


def facet_counts(
    *,
    listing_type: str | None,
    task_categories: list[str] | None,
    q: str | None,
    status: str | None,
    include_test: bool = False,
    claimed: bool | None = None,
    payment_network: str | None = None,
    max_price: float | None = None,
    has_template: bool | None = None,
    stale: bool | None = None,
) -> dict[str, Any]:
    """Counts of matching listings per task_category, listing_type, payment network and
    import source, for the same filters (including q) GET /listings accepts - so an
    agent can see what's out there before deciding how to narrow a search. Not
    paginated: a small, mostly-fixed number of buckets per dimension, not one row per
    listing. Logs q the same way list_listings does, when given."""
    conditions, params = _filter_conditions(
        status, include_test, listing_type, task_categories, claimed, payment_network, max_price, has_template, stale
    )
    where = " AND ".join(conditions)

    with _connection() as conn:
        if not q:
            return _facet_breakdowns(conn, where, params)

        ft_conditions = conditions + ["search_vector @@ websearch_to_tsquery('english', %(q)s)"]
        ft_where = " AND ".join(ft_conditions)
        ft_params = {**params, "q": q}
        ft_total = conn.execute(f"SELECT COUNT(*) AS n FROM listings WHERE {ft_where}", ft_params).fetchone()["n"]

        if ft_total > FULLTEXT_FALLBACK_THRESHOLD:
            result = _facet_breakdowns(conn, ft_where, ft_params)
        elif _trigram_available:
            with conn.transaction():
                conn.execute(f"SET LOCAL pg_trgm.word_similarity_threshold = {TRIGRAM_SIMILARITY_THRESHOLD}")
                trgm_where = " AND ".join(conditions + ["%(q)s <%% trigram_text"])
                result = _facet_breakdowns(conn, trgm_where, ft_params)
        else:
            result = _facet_breakdowns(conn, where + " AND FALSE", params)

        _log_search_query(conn, q, result["total"])
        return result


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


# ---- Imported listings (app/core/imports.py) ---------------------------------------------


def claim_listing(listing_id: str, new_submitted_by: str, now: datetime) -> dict[str, Any] | None:
    """Flips an unclaimed listing to claimed=True, submitted_by=new_submitted_by. The `NOT
    claimed` guard is in the WHERE clause (not checked separately beforehand) so two
    concurrent claims of the same listing can't both succeed. Returns None if the listing
    does not exist OR is already claimed - the route disambiguates with a plain get_listing."""
    with _connection() as conn:
        return conn.execute(
            f"UPDATE listings SET submitted_by = %(submitted_by)s, claimed = TRUE, updated_at = %(now)s "
            f"WHERE id = %(id)s AND NOT claimed RETURNING {_READ}",
            {"submitted_by": new_submitted_by, "now": now, "id": listing_id},
        ).fetchone()


def remove_imported_listing(listing_id: str, reason: str, now: datetime) -> dict[str, Any] | None:
    """Hard-deletes an imported listing (source IS NOT NULL) and records it in
    do_not_import (by source + endpoint_key) so a later sync never recreates it. Returns
    the deleted row, or None if there is no such listing or it was never imported - the
    route disambiguates with a plain get_listing."""
    with _connection() as conn:
        with conn.transaction():
            row = conn.execute(
                f"DELETE FROM listings WHERE id = %(id)s AND source IS NOT NULL RETURNING {_READ}",
                {"id": listing_id},
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "INSERT INTO do_not_import (source, endpoint_key, reason, created_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (source, endpoint_key) DO UPDATE SET reason = EXCLUDED.reason, created_at = EXCLUDED.created_at",
                (row["source"], normalize_endpoint_url(row["endpoint_url"]), reason, now),
            )
    return row


def is_in_do_not_import(source: str, endpoint_url: str) -> bool:
    endpoint_key = normalize_endpoint_url(endpoint_url)
    with _connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM do_not_import WHERE source = %s AND endpoint_key = %s", (source, endpoint_key)
        ).fetchone()
    return row is not None


#  Content columns a re-sync may refresh from source - but only while the listing is
# still unclaimed. Once claimed, the owner may have edited any of these by hand (a
# signed PATCH), and a later sync must not silently overwrite that; only the freshness
# columns (last_synced_at, missing_from_source_since) always update regardless.
# Never touched by a re-sync at all: id, source, endpoint_key/url's identity, submitted_by,
# claimed, created_at, status, is_test and imported_at (status/is_test are operator- or
# claim-owned; imported_at is "first imported", not "last imported").
_IMPORT_REFRESHABLE_COLUMNS = (
    "name", "description", "listing_type", "task_categories", "endpoint_url", "payment_wallet",
    "pricing_model", "pricing_amount", "payment_options", "erc8004_identity", "verification_agent_id",
    "source_url", "output_schema",
)


def import_upsert(row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Insert a freshly-imported listing, or - if `row["source"]` + the normalized
    endpoint_url already exist (listings_source_endpoint_uniq) - upsert it instead of
    creating a duplicate. While the existing row is still unclaimed, a resync refreshes
    its content (_IMPORT_REFRESHABLE_COLUMNS) from the latest `row`; once claimed, that
    content is the owner's and a resync leaves it alone, only touching last_synced_at and
    clearing missing_from_source_since. Returns (row, was_inserted). Caller must check
    is_in_do_not_import first."""
    row = {**row, "endpoint_key": normalize_endpoint_url(row["endpoint_url"])}
    insert_columns = _WRITE_COLUMNS
    placeholders = ", ".join(f"%({c})s" for c in insert_columns)
    refresh_clause = ", ".join(
        f"{c} = CASE WHEN listings.claimed THEN listings.{c} ELSE EXCLUDED.{c} END"
        for c in _IMPORT_REFRESHABLE_COLUMNS
    )
    with _connection() as conn:
        result = conn.execute(
            f"INSERT INTO listings ({', '.join(insert_columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT (source, endpoint_key) WHERE source IS NOT NULL DO UPDATE SET "
            f"{refresh_clause}, last_synced_at = EXCLUDED.last_synced_at, missing_from_source_since = NULL "
            f"RETURNING {_READ}, (xmax = 0) AS _inserted",
            _prepare(row),
        ).fetchone()
    inserted = result.pop("_inserted")
    return result, inserted


def preview_missing_from_source(source: str, seen_endpoint_urls: list[str]) -> list[dict[str, Any]]:
    """Read-only preview of what mark_missing_from_source(source, seen_endpoint_urls, ...)
    would mark - for a dry run, which must never write anything."""
    seen_keys = [normalize_endpoint_url(u) for u in seen_endpoint_urls]
    with _connection() as conn:
        return conn.execute(
            "SELECT id, name FROM listings WHERE source = %(source)s AND status = 'active' "
            "AND missing_from_source_since IS NULL AND NOT (endpoint_key = ANY(%(seen)s))",
            {"source": source, "seen": seen_keys},
        ).fetchall()


def mark_missing_from_source(source: str, seen_endpoint_urls: list[str], now: datetime) -> list[str]:
    """For every ACTIVE listing from `source` not present in this sync (`seen_endpoint_urls`),
    sets missing_from_source_since = now - but only the first time (rows already marked are
    left alone, so re-running a sync doesn't keep bumping the timestamp). Returns the ids
    newly marked. A listing that reappears in a later sync is un-marked by import_upsert."""
    seen_keys = [normalize_endpoint_url(u) for u in seen_endpoint_urls]
    with _connection() as conn:
        rows = conn.execute(
            "UPDATE listings SET missing_from_source_since = %(now)s "
            "WHERE source = %(source)s AND status = 'active' AND missing_from_source_since IS NULL "
            "AND NOT (endpoint_key = ANY(%(seen)s)) RETURNING id",
            {"now": now, "source": source, "seen": seen_keys},
        ).fetchall()
    return [r["id"] for r in rows]
