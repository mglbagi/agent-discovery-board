"""POST/GET/PATCH/DELETE for listings.

A note on how PATCH proves what was actually signed: the wallet signature must
cover exactly the bytes the client sent, not a server-normalized version of them
(e.g. listing_type gets lowercased before being stored) — otherwise a client that
signed their original, un-normalized JSON would fail auth against a hash computed
from the normalized version. So `_raw_body_dict` re-parses the request body exactly
as received, and that (not the validated/normalized Pydantic model) is what goes
into the signature check; the validated model is what actually gets written to the
database.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from app.core import db, score_client
from app.core.constants import TASK_CATEGORIES
from app.core.models import (
    Badge,
    ListingCreate,
    ListingResponse,
    ListingsPage,
    ListingUpdate,
    Status,
    check_pricing_consistency,
)
from app.core.rate_limit import rate_limit_listing_creation, rate_limit_listing_mutation
from app.core.wallet_auth import verify_wallet_auth

router = APIRouter()

ListingIdPath = Annotated[
    str,
    Path(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
]


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
    agent_id = row.get("verification_agent_id") or row["submitted_by"]
    raw_badge = await score_client.get_badge(agent_id)
    return ListingResponse(**row, badge=Badge(**raw_badge) if raw_badge is not None else None)


def _validate_task_category_filter(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    unknown = [c for c in values if c not in TASK_CATEGORIES]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown task_category filter value(s) {unknown!r}; must be one of {list(TASK_CATEGORIES)}",
        )
    return values


MAX_SEARCH_LIMIT = 100
MAX_SEARCH_Q_LENGTH = 200
MAX_LISTING_TYPE_FILTER_LENGTH = 50


async def search_listings(
    *,
    listing_type: str | None = None,
    task_category: list[str] | None = None,
    q: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> ListingsPage:
    """The single place that actually searches/filters listings and attaches trust
    badges. Both `GET /listings` below and the `search_listings` MCP tool
    (app/mcp_server.py) call this exact function - neither reimplements any of it.

    FastAPI's `Query(...)` constraints on the REST route already reject most bad
    input before it gets here, but the MCP tool has no equivalent of that, so the
    same bounds are re-checked here too, once, for both callers.
    """
    if listing_type is not None and len(listing_type) > MAX_LISTING_TYPE_FILTER_LENGTH:
        raise HTTPException(status_code=422, detail=f"listing_type must be at most {MAX_LISTING_TYPE_FILTER_LENGTH} characters")
    if q is not None and len(q) > MAX_SEARCH_Q_LENGTH:
        raise HTTPException(status_code=422, detail=f"q must be at most {MAX_SEARCH_Q_LENGTH} characters")
    if status is not None and status not in ("active", "inactive"):
        raise HTTPException(status_code=422, detail=f"status must be 'active' or 'inactive', got {status!r}")
    if not (1 <= limit <= MAX_SEARCH_LIMIT):
        raise HTTPException(status_code=422, detail=f"limit must be between 1 and {MAX_SEARCH_LIMIT}")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be >= 0")

    categories = _validate_task_category_filter(task_category)
    rows, total = await asyncio.to_thread(
        db.list_listings,
        listing_type=listing_type,
        task_categories=categories,
        q=q,
        status=status,
        limit=limit,
        offset=offset,
    )
    listings = await asyncio.gather(*(_to_response(row) for row in rows))
    return ListingsPage(listings=list(listings), total=total, limit=limit, offset=offset)


@router.post("/listings", response_model=ListingResponse, status_code=201, dependencies=[Depends(rate_limit_listing_creation)])
async def create_listing(payload: ListingCreate) -> ListingResponse:
    now = datetime.now(timezone.utc)
    row = {
        **payload.model_dump(),
        "id": str(uuid.uuid4()),
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    created = await asyncio.to_thread(db.create_listing, row)
    return await _to_response(created)


@router.get("/listings", response_model=ListingsPage)
async def browse_listings(
    listing_type: str | None = Query(default=None, max_length=MAX_LISTING_TYPE_FILTER_LENGTH),
    task_category: list[str] | None = Query(default=None, alias="task_category"),
    q: str | None = Query(default=None, max_length=MAX_SEARCH_Q_LENGTH),
    status: Status | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=MAX_SEARCH_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> ListingsPage:
    return await search_listings(
        listing_type=listing_type, task_category=task_category, q=q, status=status, limit=limit, offset=offset
    )


@router.get("/listings/{listing_id}", response_model=ListingResponse)
async def get_listing(listing_id: ListingIdPath) -> ListingResponse:
    row = await asyncio.to_thread(db.get_listing, listing_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No listing with this id.")
    return await _to_response(row)


@router.patch("/listings/{listing_id}", response_model=ListingResponse, dependencies=[Depends(rate_limit_listing_mutation)])
async def update_listing(listing_id: ListingIdPath, payload: ListingUpdate, request: Request) -> ListingResponse:
    existing = await asyncio.to_thread(db.get_listing, listing_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="No listing with this id.")

    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=422, detail="Patch body is empty; nothing to update.")

    raw_body = await _raw_body_dict(request)
    verify_wallet_auth(
        request, action="update-listing", listing_id=listing_id, submitted_by=existing["submitted_by"], body=raw_body
    )

    merged_type = patch.get("listing_type", existing["listing_type"])
    merged_pricing_model = patch.get("pricing_model", existing["pricing_model"])
    merged_pricing_amount = patch.get("pricing_amount", existing["pricing_amount"])
    try:
        check_pricing_consistency(merged_type, merged_pricing_model, merged_pricing_amount)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    updated = await asyncio.to_thread(
        db.update_listing, listing_id, {**patch, "updated_at": datetime.now(timezone.utc)}
    )
    return await _to_response(updated)


@router.delete("/listings/{listing_id}", response_model=ListingResponse, dependencies=[Depends(rate_limit_listing_mutation)])
async def deactivate_listing(listing_id: ListingIdPath, request: Request) -> ListingResponse:
    """Soft delete: sets status to 'inactive' rather than removing the row (the data
    model already has a status field for exactly this — see README "Design decisions:
    DELETE is a soft delete")."""
    existing = await asyncio.to_thread(db.get_listing, listing_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="No listing with this id.")

    verify_wallet_auth(
        request, action="delete-listing", listing_id=listing_id, submitted_by=existing["submitted_by"], body=None
    )

    updated = await asyncio.to_thread(db.set_listing_status, listing_id, "inactive", datetime.now(timezone.utc))
    return await _to_response(updated)
