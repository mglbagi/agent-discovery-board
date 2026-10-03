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

from app.core import db, demo_data, maintenance, score_client, verifier_info
from app.core.activity import HEARTBEAT_MIN_INTERVAL, is_stale, last_activity_at
from app.core.constants import DUPLICATE_GUARDED_LISTING_TYPE, TASK_CATEGORIES
from app.core.errors import ApiError
from app.core.models import (
    Badge,
    CompactListingResponse,
    ErrorResponse,
    FacetCounts,
    FreePath,
    HeartbeatResponse,
    ListingCreate,
    ListingNextAction,
    ListingResponse,
    ListingsPage,
    ListingUpdate,
    RemovalResponse,
    Status,
    TemplateResponse,
    VerifierInfoStatus,
    check_pricing_consistency,
    is_evm_address,
    is_valid_caip2_id,
)
from app.core.pagination import (
    encode_activity_cursor,
    encode_rank_cursor,
    encode_similarity_cursor,
    resolve_cursor,
)
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
    403: "Valid signature, but not by the expected address (error_code: wrong_signer; submitted_by, except "
    "payment_wallet for /claim and /remove-imported).",
    404: "No such listing (error_code: not_found).",
    409: "duplicate_listing (an active offering with the same normalized endpoint_url and submitted_by exists; "
    "see existing_listing_id), listing_inactive, or already_claimed.",
    422: "Validation failed (error_code: validation_error, reserved_address, invalid_test_name, not_imported "
    "or a more specific code).",
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


def _price_summary(row: dict[str, Any]) -> str | None:
    if row.get("pricing_model") == "free":
        return "free"
    if row.get("pricing_amount"):
        return row["pricing_amount"]
    for po in row.get("payment_options") or []:
        amount, unit = po.get("amount"), po.get("unit")
        if amount is not None:
            return f"{amount} {unit}" if unit else str(amount)
    return None


def _networks(row: dict[str, Any]) -> list[str]:
    return [po["network"] for po in row.get("payment_options") or []]


def _next_actions(row: dict[str, Any]) -> list[ListingNextAction]:
    """How to actually use this listing (see ListingNextAction) - always a call_service
    action, plus a verify_output action when the listing carries an output_schema."""
    from app.core.score_client import VERIFICATION_SERVICE_URL

    price, networks = _price_summary(row), _networks(row)
    actions = [
        ListingNextAction(
            action="call_service",
            description="Call this service's endpoint directly. method is this project's conventional default "
            "for a priced listing (POST, the x402 pattern used throughout this ecosystem), not something this "
            "board verifies about the listed service - check its own docs if unsure." if row.get("pricing_model")
            else "Call this service's endpoint directly; this board does not know or verify its HTTP method.",
            url=row["endpoint_url"],
            method="POST" if row.get("pricing_model") else None,
            price=price,
            networks=networks,
        )
    ]
    if row.get("output_schema") is not None:
        verification = row.get("verification") or {}
        # Field names match the verifier's own POST /verify/schema and verify_schema MCP
        # tool exactly (task_id/expected_schema/submitted_output, confirmed against its
        # live OpenAPI spec and MCP tool schema) - this board does not invent its own
        # vocabulary for a body a caller is meant to send on, unmodified apart from
        # task_id/submitted_output, to a different service.
        body = {
            "task_id": "<fill in with your own identifier for this check>",
            "expected_schema": row["output_schema"],
            "submitted_output": "<fill in with this service's actual output>",
        }
        if verification.get("rules"):
            body["rules"] = verification["rules"]
        if verification.get("bounds"):
            body["bounds"] = verification["bounds"]
        if verification.get("enforce_rules"):
            body["enforce_rules"] = True
        info = verifier_info.snapshot()
        payment, free = info["payment"], info["free_path"]
        endpoint = (payment or {}).get("endpoint") or f"{VERIFICATION_SERVICE_URL}/verify/schema"
        has_rules = "rules" in body or "bounds" in body or "enforce_rules" in body
        description = (
            f"POST this body (fill in task_id and submitted_output) to {endpoint} to check the output against "
            + ("the schema AND run its cross-field rules/bounds (enforce_rules: true), not just the schema. "
               if has_rules else "the schema. ")
        )
        if payment:
            description += (
                f"Cost: {payment['price']} {payment['currency'] or ''} per check via {payment['protocol']} on "
                f"{', '.join(payment['networks'])}. "
            ).replace("  ", " ")
        else:
            description += "Cost: not available right now (the verifier's documents have not been read yet). "
        if free:
            description += (
                f"Free path: call the {free['tool']} tool on its MCP endpoint {free['url']} - "
                f"{free['calls_per_client_per_day']} free calls per client per day"
                + (f", inputs up to {free['max_input_bytes']} bytes" if free.get("max_input_bytes") else "")
                + ". "
            )
        else:
            description += "Free path: not available right now. "
        description += (
            f"These facts come from the verifier's own documents (info.status: {info['status']}; "
            "info.sources lists them)."
        )
        actions.append(
            ListingNextAction(
                action="verify_output",
                description=description,
                url=endpoint,
                method=(payment or {}).get("method", "POST"),
                price=f"{payment['price']} {payment['currency'] or ''} per check".replace("  ", " ") if payment else None,
                networks=payment["networks"] if payment else [],
                body=body,
                protocol=payment["protocol"] if payment else None,
                free_path=FreePath(**free) if free else None,
                info=VerifierInfoStatus(status=info["status"], fetched_at=info["fetched_at"], sources=info["sources"]),
            )
        )
    return actions


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
        next_actions=_next_actions(row),
    )


def _to_compact_response(row: dict[str, Any]) -> CompactListingResponse:
    return CompactListingResponse(
        id=row["id"],
        name=row["name"],
        endpoint_url=row["endpoint_url"],
        price=_price_summary(row),
        networks=_networks(row),
        task_categories=row["task_categories"],
        claimed=row["claimed"],
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


def _validate_payment_network(value: str | None) -> str | None:
    if value is not None and not is_valid_caip2_id(value):
        raise ApiError(422, "validation_error", f"payment_network {value!r} is not a CAIP-2 chain id (namespace:reference)")
    return value


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
    claimed: bool | None = None,
    payment_network: str | None = None,
    max_price: float | None = None,
    has_template: bool | None = None,
    stale: bool | None = None,
    compact: bool = False,
) -> ListingsPage:
    """The single place that actually searches/filters listings and attaches trust
    badges. Both `GET /listings` below and the `search_listings` MCP tool
    (app/mcp_server.py) call this exact function - neither reimplements any of it.

    With no `q`: ordered newest last activity first (the latest of created_at, updated_at,
    last_seen_at), id as tiebreaker. With `q`: a natural-language full-text search (see
    app/core/db.py's list_listings) ranked by relevance instead, falling back to a
    typo-tolerant trigram match when full-text finds nothing. Either way, `cursor` resumes
    strictly after the last item of the previous page, and is bound to the exact `q` (if
    any) it was minted for.

    FastAPI's `Query(...)` constraints on the REST route already reject most bad
    input before it gets here, but the MCP tool has no equivalent of that, so the
    same bounds are re-checked here too, once, for both callers.

    Temporary `test-` listings are excluded unless include_test is set. This is also one of
    the cheap places that opportunistically purges expired test listings (throttled).

    `claimed`: omitted applies no filter; true/false restrict to claimed or unclaimed
    (imported, not yet claimed - see app/core/imports.py) listings. `payment_network`/
    `max_price`: a single payment_option must satisfy both together when both are given.
    `max_price` is a USD amount; it only ever matches a payment_option in a recognized
    USD stablecoin (app/core/stablecoins.py - currently USDC on Base/Ethereum/Solana),
    since that's the only asset type this board can compare to a dollar figure without a
    price oracle - a listing priced only in ETH, SOL or another token is excluded from
    max_price entirely, not guessed at. `has_template`: output_schema is/isn't set.
    `stale`: mirrors the `stale` response field exactly. `compact`: return
    CompactListingResponse items (id, name, endpoint_url, price, networks,
    task_categories, claimed) instead of the full shape - cheaper (no badge lookups) for
    an agent just scanning many results.
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
    if max_price is not None and max_price < 0:
        raise ApiError(422, "validation_error", "max_price must be >= 0")
    payment_network = _validate_payment_network(payment_network)

    categories = _validate_task_category_filter(task_category)
    decoded = resolve_cursor(cursor, q)
    rows, total, has_more, mode = await asyncio.to_thread(
        db.list_listings,
        listing_type=listing_type,
        task_categories=categories,
        q=q,
        status=status,
        limit=limit,
        offset=offset,
        cursor=decoded,
        include_test=include_test,
        claimed=claimed,
        payment_network=payment_network,
        max_price=max_price,
        has_template=has_template,
        stale=stale,
    )

    next_cursor = None
    if rows and has_more:
        last = rows[-1]
        if mode == "rank":
            next_cursor = encode_rank_cursor(last["_cursor_value"], last["id"], q)
        elif mode == "similarity":
            next_cursor = encode_similarity_cursor(last["_cursor_value"], last["id"], q)
        else:
            next_cursor = encode_activity_cursor(last_activity_at(last), last["id"])
    for row in rows:
        row.pop("_cursor_value", None)

    if compact:
        listings = [_to_compact_response(row) for row in rows]
    else:
        listings = list(await asyncio.gather(*(_to_response(row) for row in rows)))
    return ListingsPage(listings=listings, total=total, limit=limit, offset=offset, next_cursor=next_cursor)


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
            "submitted_by cannot be this address: either its private key is publicly known (anyone could sign "
            "for this listing) or no private key can ever sign for it at all (you would lock yourself out). "
            "Use a wallet you control.",
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
        # A listing submitted directly (not imported) is fully owned by submitted_by
        # from the start - see app/core/imports.py.
        "claimed": True,
        "source": None,
        "source_url": None,
        "imported_at": None,
        "last_synced_at": None,
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
    q: str | None = Query(
        default=None,
        max_length=MAX_SEARCH_Q_LENGTH,
        description="Natural-language search over name, description and task_categories (stemmed, typo-"
        "tolerant fallback). Switches ordering from newest-activity-first to relevance.",
    ),
    status: Status | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=MAX_SEARCH_LIMIT),
    offset: int = Query(default=0, ge=0, description="Legacy; prefer cursor."),
    cursor: str | None = Query(
        default=None,
        max_length=MAX_CURSOR_LENGTH,
        description="next_cursor from the previous page. Order: newest last activity first, or relevance when "
        "q is set. Bound to the exact q (if any) it was minted for.",
    ),
    include_test: bool = Query(
        default=False,
        description="Also include temporary test listings (names starting 'test-'), hidden by default.",
    ),
    claimed: bool | None = Query(
        default=None,
        description="Filter by claim status (see the `claimed` response field): true for claimed listings only, "
        "false for unclaimed imports only, omitted for no filter.",
    ),
    payment_network: str | None = Query(
        default=None, description="CAIP-2 chain id, e.g. 'eip155:8453'. Only listings payable on this network."
    ),
    max_price: float | None = Query(
        default=None, ge=0,
        description="A USD amount. Only matches a payment_option in a recognized USD stablecoin (currently "
        "USDC on Base/Ethereum/Solana - see the manifest's imports/search docs); listings priced only in a "
        "non-stablecoin asset (ETH, SOL, etc.) are excluded, not guessed at, since this board has no price "
        "oracle. Combined with payment_network, both must be satisfied by the same payment_option.",
    ),
    has_template: bool | None = Query(
        default=None, description="Filter by whether output_schema is set: true for listings with a declared "
        "output template, false for those without, omitted for no filter."
    ),
    stale: bool | None = Query(
        default=None, description="Filter by the `stale` response field (no activity past the threshold, or "
        "missing from its last import sync)."
    ),
    compact: bool = Query(
        default=False,
        description="Return CompactListingResponse items (id, name, endpoint_url, price, networks, "
        "task_categories, claimed) instead of the full shape - cheaper for scanning many results.",
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
        claimed=claimed,
        payment_network=payment_network,
        max_price=max_price,
        has_template=has_template,
        stale=stale,
        compact=compact,
    )


@router.get("/listings/facets", response_model=FacetCounts, responses=_errors(422, 429))
async def facets(
    listing_type: str | None = Query(default=None, max_length=MAX_LISTING_TYPE_FILTER_LENGTH),
    task_category: list[str] | None = Query(default=None, alias="task_category"),
    q: str | None = Query(default=None, max_length=MAX_SEARCH_Q_LENGTH),
    status: Status | None = Query(default=None),
    include_test: bool = Query(default=False),
    claimed: bool | None = Query(default=None),
    payment_network: str | None = Query(default=None),
    max_price: float | None = Query(
        default=None, ge=0, description="A USD amount; see the same parameter on GET /listings."
    ),
    has_template: bool | None = Query(default=None),
    stale: bool | None = Query(default=None),
) -> FacetCounts:
    """Counts of matching listings per task_category, listing_type, payment network and
    import source - the same filters (including q) as GET /listings, so an agent can see
    what's out there before deciding how to narrow a search instead of paging through
    everything. Not paginated: a small, mostly-fixed number of buckets per dimension."""
    return await list_facets(
        listing_type=listing_type, task_category=task_category, q=q, status=status, include_test=include_test,
        claimed=claimed, payment_network=payment_network, max_price=max_price, has_template=has_template, stale=stale,
    )


async def list_facets(
    *,
    listing_type: str | None = None,
    task_category: list[str] | None = None,
    q: str | None = None,
    status: str | None = None,
    include_test: bool = False,
    claimed: bool | None = None,
    payment_network: str | None = None,
    max_price: float | None = None,
    has_template: bool | None = None,
    stale: bool | None = None,
) -> FacetCounts:
    """Shared by GET /listings/facets and the list_facets MCP tool - same validation and
    filters as search_listings above, minus pagination (facets are never paginated)."""
    await asyncio.to_thread(maintenance.maybe_purge)
    if listing_type is not None and len(listing_type) > MAX_LISTING_TYPE_FILTER_LENGTH:
        raise ApiError(422, "validation_error", f"listing_type must be at most {MAX_LISTING_TYPE_FILTER_LENGTH} characters")
    if q is not None and len(q) > MAX_SEARCH_Q_LENGTH:
        raise ApiError(422, "validation_error", f"q must be at most {MAX_SEARCH_Q_LENGTH} characters")
    if status is not None and status not in ("active", "inactive"):
        raise ApiError(422, "validation_error", f"status must be 'active' or 'inactive', got {status!r}")
    if max_price is not None and max_price < 0:
        raise ApiError(422, "validation_error", "max_price must be >= 0")
    payment_network = _validate_payment_network(payment_network)
    categories = _validate_task_category_filter(task_category)

    counts = await asyncio.to_thread(
        db.facet_counts,
        listing_type=listing_type,
        task_categories=categories,
        q=q,
        status=status,
        include_test=include_test,
        claimed=claimed,
        payment_network=payment_network,
        max_price=max_price,
        has_template=has_template,
        stale=stale,
    )
    return FacetCounts(**counts)


@router.get("/listings/{listing_id}", response_model=ListingResponse, responses=_errors(404))
async def get_listing(listing_id: ListingIdPath) -> ListingResponse:
    row = await asyncio.to_thread(db.get_listing, listing_id)
    if row is None:
        raise ApiError(404, "not_found", "No listing with this id.")
    return await _to_response(row)


@router.get(
    "/listings/{listing_id}/template",
    response_model=TemplateResponse,
    responses=_errors(404),
)
async def get_template(listing_id: ListingIdPath) -> TemplateResponse:
    """The full verification template a listing has declared: its output_schema, plus
    verification (rules/bounds/enforce_rules) and template_url when set - see
    ListingNextAction's verify_output, which carries the same rules/bounds into its
    suggested body. Most useful for a verification_profile listing but available on any
    listing that has one. 404 no_template if the listing has no output_schema (the
    required part of a template), or not_found if it doesn't exist."""
    row = await asyncio.to_thread(db.get_listing, listing_id)
    if row is None:
        raise ApiError(404, "not_found", "No listing with this id.")
    if row.get("output_schema") is None:
        raise ApiError(404, "no_template", "This listing has no output_schema set.")
    return TemplateResponse(
        listing_id=listing_id,
        output_schema=row["output_schema"],
        verification=row.get("verification"),
        template_url=row.get("template_url"),
    )


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
    "/listings/{listing_id}/claim",
    response_model=ListingResponse,
    dependencies=[Depends(rate_limit_listing_mutation)],
    responses=_errors(401, 403, 404, 409, 429),
)
async def claim_listing(listing_id: ListingIdPath, request: Request) -> ListingResponse:
    """For an imported, unclaimed listing (see app/core/imports.py): the real owner
    proves control by signing with the wallet that matches payment_wallet - the same
    EIP-191 scheme as PATCH, action `claim-listing`, no body. On success submitted_by
    becomes that address and claimed becomes true, and the listing behaves exactly like
    any other from then on. 409 already_claimed if it was claimed already (by anyone,
    imported or not)."""
    existing = await asyncio.to_thread(db.get_listing, listing_id)
    if existing is None:
        raise ApiError(404, "not_found", "No listing with this id.")
    if existing["claimed"]:
        raise ApiError(409, "already_claimed", "This listing has already been claimed.")
    if not is_evm_address(existing["payment_wallet"]):
        raise ApiError(
            422,
            "unclaimable_payment_wallet",
            "This listing's payment_wallet is not an EVM address; claiming is EVM-only for now.",
        )

    verify_wallet_auth(
        request,
        action="claim-listing",
        listing_id=listing_id,
        submitted_by=existing["payment_wallet"],
        body=None,
        expected_role="payment_wallet",
    )

    updated = await asyncio.to_thread(db.claim_listing, listing_id, existing["payment_wallet"], datetime.now(timezone.utc))
    if updated is None:  # lost a race with a concurrent claim
        raise ApiError(409, "already_claimed", "This listing has already been claimed.")
    return await _to_response(updated)


@router.post(
    "/listings/{listing_id}/remove-imported",
    response_model=RemovalResponse,
    dependencies=[Depends(rate_limit_listing_mutation)],
    responses=_errors(401, 403, 404, 422, 429),
)
async def remove_imported_listing(listing_id: ListingIdPath, request: Request) -> RemovalResponse:
    """For an imported listing (see app/core/imports.py): the real pay-to owner signs to
    have it removed immediately, whether or not it has been claimed yet - same scheme as
    PATCH, action `remove-imported-listing`, no body, verified against payment_wallet.
    Hard-deletes the listing and records its (source, endpoint) in a do-not-import list,
    so a later sync never recreates it. 422 not_imported if source is not set (use the
    normal signed DELETE for a listing that was not imported)."""
    existing = await asyncio.to_thread(db.get_listing, listing_id)
    if existing is None:
        raise ApiError(404, "not_found", "No listing with this id.")
    if existing["source"] is None:
        raise ApiError(422, "not_imported", "This listing was not imported; use the normal signed DELETE instead.")
    if not is_evm_address(existing["payment_wallet"]):
        raise ApiError(
            422,
            "unclaimable_payment_wallet",
            "This listing's payment_wallet is not an EVM address; self-service removal is EVM-only for now.",
        )

    verify_wallet_auth(
        request,
        action="remove-imported-listing",
        listing_id=listing_id,
        submitted_by=existing["payment_wallet"],
        body=None,
        expected_role="payment_wallet",
    )

    removed = await asyncio.to_thread(
        db.remove_imported_listing, listing_id, "removed by pay-to owner", datetime.now(timezone.utc)
    )
    if removed is None:
        raise ApiError(404, "not_found", "No listing with this id.")
    return RemovalResponse(id=listing_id, removed=True, do_not_import=True)


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
