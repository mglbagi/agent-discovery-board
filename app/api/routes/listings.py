"""POST/GET/PATCH/DELETE for listings, plus the signed heartbeat.

A note on how PATCH proves what was actually signed: the wallet signature must
cover exactly the bytes the client sent, not a server-normalized version of them
(e.g. listing_type gets lowercased before being stored) — otherwise a client that
signed their original, un-normalized JSON would fail auth against a hash computed
from the normalized version. So `_raw_body_dict` re-parses the request body exactly
as received, and that (not the validated/normalized Pydantic model) is what goes
into the signature check; the validated model is what actually gets written to the
database.

Every error raised here is an ApiError or plain HTTPException; app/core/errors.py
turns all of them into the one machine-readable error shape.
"""

import asyncio
import json
import math
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request

from app.core import db, demo_data, maintenance, score_client
from app.core.activity import HEARTBEAT_MIN_INTERVAL, is_stale, last_activity_at
from app.core.constants import DUPLICATE_GUARDED_LISTING_TYPE, TASK_CATEGORIES
from app.core.errors import ApiError
from app.core.models import (
    Badge,
    ErrorResponse,
    HeartbeatResponse,
    ListingCreate,
    ListingResponse,
    ListingsPage,
    ListingUpdate,
    Status,
    check_pricing_consistency,
)
from app.core.pagination import decode_cursor, encode_cursor
from app.core.rate_limit import rate_limit_listing_creation, rate_limit_listing_mutation
from app.core.reserved import is_reserved_address
from app.core.wallet_auth import verify_wallet_auth

router = APIRouter()

ListingIdPath = Annotated[
    str,
    Path(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
]

_ERROR_DESCRIPTIONS = {
    401: "Signature missing, malformed, stale, replayed or invalid (error_code says which).",
    403: "Valid signature, but not by the listing's submitted_by (error_code: wrong_signer).",
    404: "No such listing (error_code: not_found).",
    409: "duplicate_listing (an active offering with the same normalized endpoint_url and submitted_by exists; "
    "see existing_listing_id) or listing_inactive.",
    422: "Validation failed (error_code: validation_error, reserved_address, invalid_test_name or a more "
    "specific code).",
    429: "rate_limited; see retry_after.",
}


def _errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {s: {"model": ErrorResponse, "description": _ERROR_DESCRIPTIONS[s]} for s in statuses}


async def _raw_body_dict(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}  # unreachable in practice: FastAPI would already have 422'd on an unparseable body
    return parsed if isinstance(parsed, dict) else {}


async def _to_response(row: dict[str, Any]) -> ListingResponse:
    # Test listings never cost a trust-score lookup.
    if row["is_test"]:
        raw_badge = None
    else:
        raw_badge = await score_client.get_badge(row.get("verification_agent_id") or row["submitted_by"])
    return ListingResponse(
        **row,
        test=row["is_test"],
        expires_at=demo_data.expires_at(row["created_at"]) if row["is_test"] else None,
        last_activity_at=last_activity_at(row),
        stale=is_stale(row),
        badge=Badge(**raw_badge) if raw_badge is not None else None,
    )


def _validate_task_category_filter(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    unknown = [c for c in values if c not in TASK_CATEGORIES]
    if unknown:
        raise ApiError(
            422,
            "invalid_task_category",
            f"unknown task_category filter value(s) {unknown!r}; must be one of {list(TASK_CATEGORIES)}",
        )
    return values


MAX_SEARCH_LIMIT = 100
MAX_SEARCH_Q_LENGTH = 200
MAX_LISTING_TYPE_FILTER_LENGTH = 50
MAX_CURSOR_LENGTH = 512


async def search_listings(
    *,
    listing_type: str | None = None,
    task_category: list[str] | None = None,
    q: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
    cursor: str | None = None,
    include_test: bool = False,
) -> ListingsPage:
    """The single place that actually searches/filters listings and attaches trust
    badges. Both `GET /listings` below and the `search_listings` MCP tool
    (app/mcp_server.py) call this exact function - neither reimplements any of it.

    Results are ordered newest last activity first (the latest of created_at,
    updated_at, last_seen_at), id as tiebreaker; `cursor` resumes strictly after the
    last item of the previous page.

    FastAPI's `Query(...)` constraints on the REST route already reject most bad
    input before it gets here, but the MCP tool has no equivalent of that, so the
    same bounds are re-checked here too, once, for both callers.

    Temporary `test-` listings are excluded unless include_test is set. This is also one of
    the cheap places that opportunistically purges expired test listings (throttled).
    """
    await asyncio.to_thread(maintenance.maybe_purge)
    if listing_type is not None and len(listing_type) > MAX_LISTING_TYPE_FILTER_LENGTH:
        raise ApiError(422, "validation_error", f"listing_type must be at most {MAX_LISTING_TYPE_FILTER_LENGTH} characters")
    if q is not None and len(q) > MAX_SEARCH_Q_LENGTH:
        raise ApiError(422, "validation_error", f"q must be at most {MAX_SEARCH_Q_LENGTH} characters")
    if status is not None and status not in ("active", "inactive"):
        raise ApiError(422, "validation_error", f"status must be 'active' or 'inactive', got {status!r}")
    if not (1 <= limit <= MAX_SEARCH_LIMIT):
        raise ApiError(422, "validation_error", f"limit must be between 1 and {MAX_SEARCH_LIMIT}")
    if offset < 0:
        raise ApiError(422, "validation_error", "offset must be >= 0")
    if cursor is not None and offset:
        raise ApiError(422, "invalid_pagination", "cursor and offset cannot be combined; use only the cursor.")
    if cursor is not None and len(cursor) > MAX_CURSOR_LENGTH:
        raise ApiError(422, "invalid_cursor", "cursor is not a valid cursor returned by this service.")

    categories = _validate_task_category_filter(task_category)
    decoded = decode_cursor(cursor) if cursor is not None else None
    rows, total, has_more = await asyncio.to_thread(
        db.list_listings,
        listing_type=listing_type,
        task_categories=categories,
        q=q,
        status=status,
        limit=limit,
        offset=offset,
        cursor=decoded,
        include_test=include_test,
    )
    listings = await asyncio.gather(*(_to_response(row) for row in rows))
    next_cursor = encode_cursor(last_activity_at(rows[-1]), rows[-1]["id"]) if rows and has_more else None
    return ListingsPage(listings=list(listings), total=total, limit=limit, offset=offset, next_cursor=next_cursor)


def _duplicate_error(existing_id: str, is_test: bool = False) -> ApiError:
    return ApiError(
        409,
        "duplicate_listing",
        "An active offering with the same normalized endpoint_url and submitted_by already exists"
        + (" (among test listings)" if is_test else "")
        + ". Nothing was created or changed.",
        extras={"existing_listing_id": existing_id},
    )


@router.post(
    "/listings",
    response_model=ListingResponse,
    status_code=201,
    dependencies=[Depends(rate_limit_listing_creation)],
    responses=_errors(409, 422, 429),
)
async def create_listing(payload: ListingCreate) -> ListingResponse:
    """Create a listing. Unsigned and never modifies an existing listing: if this is an
    offering and an ACTIVE offering with the same normalized endpoint_url and
    submitted_by exists, this returns 409 duplicate_listing (with existing_listing_id and
    next_actions) and writes nothing. The same endpoint from a different submitted_by is
    allowed, and announcements, notices and requests may repeat freely. Addresses whose
    private keys are public are refused as submitted_by (422 reserved_address)."""
    if is_reserved_address(payload.submitted_by):
        raise ApiError(
            422,
            "reserved_address",
            "submitted_by is a publicly known example address (its private key is public), so anyone could sign "
            "for this listing. Use a wallet you control.",
        )
    is_test = demo_data.is_test_name(payload.name)
    await asyncio.to_thread(maintenance.maybe_purge)
    if payload.listing_type == DUPLICATE_GUARDED_LISTING_TYPE:
        existing_id = await asyncio.to_thread(
            db.find_active_duplicate, payload.endpoint_url, payload.submitted_by, None, is_test
        )
        if existing_id is not None:
            raise _duplicate_error(existing_id, is_test)

    now = datetime.now(timezone.utc)
    row = {
        **payload.model_dump(),
        "id": str(uuid.uuid4()),
        "status": "active",
        "is_test": is_test,
        "created_at": now,
        "updated_at": now,
    }
    try:
        created = await asyncio.to_thread(db.create_listing, row)
    except db.UniqueViolation:  # lost a race with a concurrent identical POST
        existing_id = await asyncio.to_thread(
            db.find_active_duplicate, payload.endpoint_url, payload.submitted_by, None, is_test
        )
        raise _duplicate_error(existing_id or "unknown", is_test) from None
    return await _to_response(created)


@router.get("/listings", response_model=ListingsPage, responses=_errors(422, 429))
async def browse_listings(
    listing_type: str | None = Query(default=None, max_length=MAX_LISTING_TYPE_FILTER_LENGTH),
    task_category: list[str] | None = Query(default=None, alias="task_category"),
    q: str | None = Query(default=None, max_length=MAX_SEARCH_Q_LENGTH),
    status: Status | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=MAX_SEARCH_LIMIT),
    offset: int = Query(default=0, ge=0, description="Legacy; prefer cursor."),
    cursor: str | None = Query(
        default=None,
        max_length=MAX_CURSOR_LENGTH,
        description="next_cursor from the previous page. Order: newest last activity first.",
    ),
    include_test: bool = Query(
        default=False,
        description="Also include temporary test listings (names starting 'test-'), hidden by default.",
    ),
) -> ListingsPage:
    return await search_listings(
        listing_type=listing_type,
        task_category=task_category,
        q=q,
        status=status,
        limit=limit,
        offset=offset,
        cursor=cursor,
        include_test=include_test,
    )


@router.get("/listings/{listing_id}", response_model=ListingResponse, responses=_errors(404))
async def get_listing(listing_id: ListingIdPath) -> ListingResponse:
    row = await asyncio.to_thread(db.get_listing, listing_id)
    if row is None:
        raise ApiError(404, "not_found", "No listing with this id.")
    return await _to_response(row)


@router.patch(
    "/listings/{listing_id}",
    response_model=ListingResponse,
    dependencies=[Depends(rate_limit_listing_mutation)],
    responses=_errors(401, 403, 404, 409, 422, 429),
)
async def update_listing(listing_id: ListingIdPath, payload: ListingUpdate, request: Request) -> ListingResponse:
    existing = await asyncio.to_thread(db.get_listing, listing_id)
    if existing is None:
        raise ApiError(404, "not_found", "No listing with this id.")

    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise ApiError(422, "empty_patch", "Patch body is empty; nothing to update.")
    if "name" in patch and demo_data.is_test_name(patch["name"]) != existing["is_test"]:
        raise ApiError(
            422,
            "invalid_test_name",
            "The 'test-' name prefix marks a listing as temporary test data; it can only be set when a listing "
            "is created and cannot be added to or removed from an existing listing's name.",
        )

    raw_body = await _raw_body_dict(request)
    verify_wallet_auth(
        request, action="update-listing", listing_id=listing_id, submitted_by=existing["submitted_by"], body=raw_body
    )

    merged_type = patch.get("listing_type", existing["listing_type"])
    merged_pricing_model = patch.get("pricing_model", existing["pricing_model"])
    merged_pricing_amount = patch.get("pricing_amount", existing["pricing_amount"])
    merged_payment_options = patch.get("payment_options", existing["payment_options"])
    try:
        check_pricing_consistency(merged_type, merged_pricing_model, merged_pricing_amount, merged_payment_options)
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc

    merged_status = patch.get("status", existing["status"])
    if (
        merged_type == DUPLICATE_GUARDED_LISTING_TYPE
        and merged_status == "active"
        and {"endpoint_url", "status", "listing_type"} & patch.keys()
    ):
        clash = await asyncio.to_thread(
            db.find_active_duplicate,
            patch.get("endpoint_url", existing["endpoint_url"]),
            existing["submitted_by"],
            listing_id,
            existing["is_test"],
        )
        if clash is not None:
            raise _duplicate_error(clash, existing["is_test"])

    try:
        updated = await asyncio.to_thread(
            db.update_listing, listing_id, {**patch, "updated_at": datetime.now(timezone.utc)}
        )
    except db.UniqueViolation:
        clash = await asyncio.to_thread(
            db.find_active_duplicate,
            patch.get("endpoint_url", existing["endpoint_url"]),
            existing["submitted_by"],
            listing_id,
            existing["is_test"],
        )
        raise _duplicate_error(clash or "unknown", existing["is_test"]) from None
    return await _to_response(updated)


@router.delete(
    "/listings/{listing_id}",
    response_model=ListingResponse,
    dependencies=[Depends(rate_limit_listing_mutation)],
    responses=_errors(401, 403, 404, 429),
)
async def deactivate_listing(listing_id: ListingIdPath, request: Request) -> ListingResponse:
    """Soft delete: sets status to 'inactive' rather than removing the row (the data
    model already has a status field for exactly this — see README "Design decisions:
    DELETE is a soft delete")."""
    existing = await asyncio.to_thread(db.get_listing, listing_id)
    if existing is None:
        raise ApiError(404, "not_found", "No listing with this id.")

    verify_wallet_auth(
        request, action="delete-listing", listing_id=listing_id, submitted_by=existing["submitted_by"], body=None
    )

    updated = await asyncio.to_thread(db.set_listing_status, listing_id, "inactive", datetime.now(timezone.utc))
    return await _to_response(updated)


@router.post(
    "/listings/{listing_id}/heartbeat",
    response_model=HeartbeatResponse,
    dependencies=[Depends(rate_limit_listing_mutation)],
    responses=_errors(401, 403, 404, 409, 429),
)
async def heartbeat_listing(listing_id: ListingIdPath, request: Request) -> HeartbeatResponse:
    """Signed proof-of-life from the listing's owner: sets last_seen_at, which feeds the
    default sort and the `stale` flag. Same wallet-signature scheme as PATCH (action
    `heartbeat-listing`, no body, replay-protected); at most once per 24 hours per
    listing (429 rate_limited with retry_after). Only active listings can heartbeat."""
    existing = await asyncio.to_thread(db.get_listing, listing_id)
    if existing is None:
        raise ApiError(404, "not_found", "No listing with this id.")

    verify_wallet_auth(
        request, action="heartbeat-listing", listing_id=listing_id, submitted_by=existing["submitted_by"], body=None
    )

    if existing["status"] != "active":
        raise ApiError(409, "listing_inactive", "This listing is inactive; reactivate it before sending a heartbeat.")

    now = datetime.now(timezone.utc)
    updated = await asyncio.to_thread(db.record_heartbeat, listing_id, now, now - HEARTBEAT_MIN_INTERVAL)
    if updated is None:
        current = await asyncio.to_thread(db.get_listing, listing_id)
        seen = (current or existing).get("last_seen_at") or now
        wait = max(1, math.ceil((seen + HEARTBEAT_MIN_INTERVAL - now).total_seconds()))
        raise ApiError(
            429,
            "rate_limited",
            f"A heartbeat was already recorded for this listing within the last "
            f"{int(HEARTBEAT_MIN_INTERVAL.total_seconds() // 3600)} hours.",
            headers={"Retry-After": str(wait)},
            extras={"retry_after": wait},
        )
    return HeartbeatResponse(
        id=updated["id"],
        last_seen_at=updated["last_seen_at"],
        next_heartbeat_allowed_at=updated["last_seen_at"] + HEARTBEAT_MIN_INTERVAL,
        last_activity_at=last_activity_at(updated),
        stale=is_stale(updated),
    )
