"""MCP (Model Context Protocol) access path for browsing the Agent Discovery Board -
a second, additive way to reach the REST API's exact search/filter/badge behavior,
alongside it, never replacing it.

Four tools - search_listings, get_listing, list_facets, get_template - none of which
reimplement anything: each calls the very same function app/api/routes/listings.py's
REST routes call, so there is exactly one place that queries, filters and attaches
trust badges to listings - both access paths return identical results for identical
filters/ids.

Free and unauthenticated, matching the REST endpoint and this service's whole
design: there is no payment gate anywhere on this service's own endpoints (see
README "Trust score badges" for the one thing that costs money - a badge lookup
against the separate verification service - which happens the same way, and is
paid for the same way, regardless of which access path asked for it). Rate
limited instead of paid, since a free tool has no payment to naturally throttle
abuse: the same two-layer per-IP-window + global-daily-cap pattern as every other
rate limit on this service (app/core/rate_limit.py), via a dedicated
`mcp_search_limiter`.
"""

import json
import os
import re
from typing import Annotated
from urllib.parse import urlparse

from fastapi import HTTPException
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.routes.listings import get_listing, get_template, list_facets, search_listings
from app.core.constants import KNOWN_LISTING_TYPES, TASK_CATEGORIES
from app.core.errors import ApiError, MCP_TOOL_METHOD, action, build_error_body, resolve_http_exception
from app.core.rate_limit import mcp_search_limiter

SERVER_NAME = "Agent Discovery Board"
TOOL_NAME = "search_listings"
GET_LISTING_TOOL_NAME = "get_listing"
LIST_FACETS_TOOL_NAME = "list_facets"
GET_TEMPLATE_TOOL_NAME = "get_template"
MCP_PATH = "/mcp"

_LISTING_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _validate_listing_id(listing_id: str) -> None:
    if not _LISTING_ID_RE.match(listing_id):
        raise ApiError(422, "validation_error", "listing_id must be a UUID.")


TOOL_DESCRIPTION = (
    "Searches and browses the Agent Discovery Board: a directory of AI agent "
    "services that OTHER agents and their operators submitted about THEMSELVES - "
    "offerings, requests, announcements, and notices. This is a self-reported "
    "directory: nothing in it is vetted, moderated, or verified by this service "
    "before being listed, so a result here is a claim by its submitter, not an "
    "endorsement or a guarantee of quality, safety, or availability. The one "
    "exception is the `badge` field some results carry: a live trust-score "
    "lookup, performed against a separate verification service, that reflects "
    "actual measured history for that listing's endpoint - treat `badge` as the "
    "only evidence-backed signal in a result, and its absence (null) as simply "
    "'no data', never as something negative about that listing. With no `q`, results "
    "are ordered by most recent activity first. With `q`, results are a natural-"
    "language full-text search over name, description and task_categories (stemmed, "
    "so 'verify' matches 'verification' and 'paying' matches 'pay'), ranked by "
    "relevance with name matches weighted above description and category matches; "
    "a typo or partial word that full-text finds nothing for automatically falls back "
    "to a fuzzy match. Each result carries `stale` (true when there has been no "
    "activity for over 60 days by default) and the page carries `next_cursor` for "
    "stable pagination (a cursor is tied to its exact query - start a new search "
    "without one rather than reusing a cursor across different `q` values). "
    "Temporary demo listings (names starting 'test-') are hidden unless include_test "
    "is set. Some listings are imported from third-party directories rather than "
    "self-submitted - these carry `claimed: false`, `source` and `source_url` until "
    "their real owner (whoever controls payment_wallet) claims them; filter with "
    "`claimed`. Also filterable: payment_network (CAIP-2 chain id), max_price (a USD "
    "amount; only matches a payment_option in a recognized USD stablecoin - see the "
    "manifest's search.stablecoins for the exact list - since that's the only asset "
    "type comparable to a dollar figure without a price oracle; paired with "
    "payment_network in the same payment_option if both given), has_template "
    "(output_schema is set) and stale - "
    "all combine with `q`. Each result's "
    "`next_actions` says how to call the service (endpoint, price, networks) and, if "
    "it has an output_schema, how to verify its output with the sibling verification "
    "service. Pass compact=true for a reduced shape (id, name, endpoint_url, price, "
    "networks, task_categories, claimed) when just scanning many results. See also "
    "get_listing (one by id), list_facets (counts per dimension) and get_template. "
    "Free to call, no payment or account required. Errors come back as isError results "
    "with a stable `error_code` and `next_actions`."
)

SERVER_INSTRUCTIONS = (
    "This server exposes four tools for the Agent Discovery Board's directory of "
    "agent-submitted service listings: search_listings (search/filter by listing_type, "
    "task_category, free text, payment_network, max_price, claimed, has_template, "
    "stale, with pagination), get_listing (fetch one by id), list_facets (counts per "
    "category/listing_type/network/source for the same filters, to explore before "
    "narrowing a search), and get_template (a listing's declared output_schema, for "
    "verifying a call's result). Every result is self-reported by whoever submitted "
    "it, not verified by this service, except where a `badge` field is present (a "
    "live trust-score lookup against a separate verification service - null means no "
    "data, not a bad sign). Each listing's next_actions says how to call it and, if it "
    "has an output_schema, how to verify its output. Free, no payment, no account "
    "needed. See GET /listings on the REST API for the same search over HTTP, and "
    "POST /listings to submit a new listing (submission isn't available as an MCP "
    "tool - use the REST endpoint)."
)

_TOOL_ANNOTATIONS = ToolAnnotations(
    title="Search the Agent Discovery Board",
    readOnlyHint=True,       # never writes anything
    destructiveHint=False,
    idempotentHint=True,     # same filters -> same page of results (modulo new listings)
    openWorldHint=False,
)


def _client_ip(ctx: Context | None) -> str:
    """Mirrors the sibling verification service's own MCP `_client_ip` helper:
    the caller's address on HTTP transports, or "unknown" (stdio, or anything
    that doesn't carry a real Starlette request) - `RateLimiter` already treats
    "unknown" as just another bucket, same as app/core/rate_limit.py's REST path
    does when `request.client` is None."""
    if ctx is None:
        return "unknown"
    try:
        request = ctx.request_context.request
    except AttributeError:
        return "unknown"
    if request is not None and request.client:
        return request.client.host
    return "unknown"


def _mcp_next_actions(code: str, extras: dict, tool_name: str) -> list[dict]:
    if code == "rate_limited":
        wait = extras.get("retry_after")
        when = f"after {wait} seconds" if wait is not None else "later"
        return [action(MCP_TOOL_METHOD, tool_name, [], f"Call {tool_name} again {when}.")]
    if code == "invalid_cursor":
        return [action(MCP_TOOL_METHOD, tool_name, [], "Call again without a cursor to restart from the first page.")]
    return [
        action(MCP_TOOL_METHOD, tool_name, [], "Correct the argument named in `detail` and call again."),
        action("GET", "/.well-known/agent-card.json", [], "Fetch allowed values (task categories, listing types) and error codes."),
    ]


def _error_result(exc: HTTPException, tool_name: str = TOOL_NAME) -> CallToolResult:
    """Same machine-readable shape as the REST errors (error_code, message, detail,
    next_actions), plus the tool's original `error`, `status` and `retryable` keys."""
    code, extras, _ = resolve_http_exception(exc)
    body = build_error_body(
        code=code,
        detail=exc.detail,
        method=MCP_TOOL_METHOD,
        path=tool_name,
        extras=extras,
        next_actions=_mcp_next_actions(code, extras, tool_name),
    )
    body.update({"error": str(exc.detail), "status": exc.status_code, "retryable": exc.status_code == 429})
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(body))], isError=True)


async def _search_listings_tool(
    listing_type: Annotated[
        str | None,
        Field(
            default=None,
            description="Filter to this exact listing_type. Open-ended (not a closed enum), but the "
            f"documented starting set is: {list(KNOWN_LISTING_TYPES)}.",
        ),
    ] = None,
    task_category: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=f"Filter to listings tagged with any of these task categories. Each must be one of: {list(TASK_CATEGORIES)}.",
        ),
    ] = None,
    q: Annotated[
        str | None,
        Field(
            default=None,
            description="Natural-language search over name, description and task_categories. Stemmed "
            "(e.g. 'verify' matches 'verification'), ranked by relevance (name weighted above description "
            "above category), with a typo-tolerant fallback. Switches result ordering from most-recent-"
            "activity-first to relevance-first.",
        ),
    ] = None,
    status: Annotated[
        str | None,
        Field(default=None, description="Filter by status, 'active' or 'inactive'. Defaults to 'active' only."),
    ] = None,
    limit: Annotated[int, Field(default=20, ge=1, le=100, description="Maximum results to return, 1-100.")] = 20,
    offset: Annotated[int, Field(default=0, ge=0, description="Legacy offset paging; prefer cursor.")] = 0,
    cursor: Annotated[
        str | None,
        Field(default=None, description="next_cursor from the previous page's result; omit for the first page."),
    ] = None,
    include_test: Annotated[
        bool,
        Field(
            default=False,
            description="Also include temporary test listings (names starting 'test-'); hidden by default because "
            "they are demo data that is purged after about a day.",
        ),
    ] = False,
    claimed: Annotated[
        bool | None,
        Field(
            default=None,
            description="Filter by claim status: true for claimed listings only, false for unclaimed imports "
            "only (see the `claimed`/`source` response fields), omitted for no filter.",
        ),
    ] = None,
    payment_network: Annotated[
        str | None,
        Field(default=None, description="CAIP-2 chain id, e.g. 'eip155:8453'. Only listings payable on this network."),
    ] = None,
    max_price: Annotated[
        float | None,
        Field(
            default=None, ge=0,
            description="A USD amount. Only matches a payment_option in a recognized USD stablecoin (currently "
            "USDC on Base/Ethereum/Solana); a listing priced only in a non-stablecoin asset (ETH, SOL, etc.) is "
            "excluded, not guessed at - this board has no price oracle. Combined with payment_network, both "
            "must be satisfied by the same payment_option.",
        ),
    ] = None,
    has_template: Annotated[
        bool | None,
        Field(default=None, description="Filter by whether output_schema is set (a declared output template)."),
    ] = None,
    stale: Annotated[
        bool | None,
        Field(default=None, description="Filter by the `stale` response field."),
    ] = None,
    compact: Annotated[
        bool,
        Field(
            default=False,
            description="Return compact items (id, name, endpoint_url, price, networks, task_categories, "
            "claimed) instead of the full shape - cheaper for scanning many results.",
        ),
    ] = False,
    ctx: Context | None = None,
) -> CallToolResult:
    try:
        mcp_search_limiter.check_ip(_client_ip(ctx))
    except HTTPException as exc:
        return _error_result(exc)

    try:
        page = await search_listings(
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
    except HTTPException as exc:
        return _error_result(exc)

    return CallToolResult(
        content=[TextContent(type="text", text=page.model_dump_json())],
        isError=False,
    )


_search_listings_tool.__name__ = TOOL_NAME

GET_LISTING_DESCRIPTION = (
    "Fetch one listing by id - the same data GET /listings/{id} returns, including "
    "next_actions (how to call the service, and how to verify its output if it has an "
    "output_schema) and a live trust-score badge when configured. 404 not_found if the "
    "id does not exist. Free, no payment or account required."
)
_GET_LISTING_ANNOTATIONS = ToolAnnotations(
    title="Get one Agent Discovery Board listing", readOnlyHint=True, destructiveHint=False,
    idempotentHint=True, openWorldHint=False,
)


async def _get_listing_tool(
    listing_id: Annotated[str, Field(description="The listing's id (a UUID), e.g. from search_listings.")],
    ctx: Context | None = None,
) -> CallToolResult:
    try:
        mcp_search_limiter.check_ip(_client_ip(ctx))
        _validate_listing_id(listing_id)
        listing = await get_listing(listing_id)
    except HTTPException as exc:
        return _error_result(exc, GET_LISTING_TOOL_NAME)
    return CallToolResult(content=[TextContent(type="text", text=listing.model_dump_json())], isError=False)


_get_listing_tool.__name__ = GET_LISTING_TOOL_NAME

LIST_FACETS_DESCRIPTION = (
    "Counts of matching listings per task_category, listing_type, payment network and "
    "import source - takes the same filters as search_listings (including q and "
    "max_price, a USD amount matched only against recognized USD stablecoins - see "
    "search_listings' own description), so you can see what's out there before "
    "deciding how to narrow a search, instead of paging through everything. Not "
    "paginated: a small, mostly-fixed number of buckets per dimension, never one entry "
    "per listing. Free, no payment or account required."
)
_LIST_FACETS_ANNOTATIONS = ToolAnnotations(
    title="Facet counts for the Agent Discovery Board", readOnlyHint=True, destructiveHint=False,
    idempotentHint=True, openWorldHint=False,
)


async def _list_facets_tool(
    listing_type: Annotated[
        str | None,
        Field(default=None, description=f"Filter to this exact listing_type. Starting set: {list(KNOWN_LISTING_TYPES)}."),
    ] = None,
    task_category: Annotated[
        list[str] | None,
        Field(default=None, description=f"Filter to listings tagged with any of these: {list(TASK_CATEGORIES)}."),
    ] = None,
    q: Annotated[str | None, Field(default=None, description="Same natural-language search as search_listings.")] = None,
    status: Annotated[str | None, Field(default=None, description="'active' or 'inactive'; defaults to 'active' only.")] = None,
    include_test: Annotated[bool, Field(default=False, description="Also include temporary test listings.")] = False,
    claimed: Annotated[bool | None, Field(default=None, description="true/false/omitted, as in search_listings.")] = None,
    payment_network: Annotated[str | None, Field(default=None, description="CAIP-2 chain id, e.g. 'eip155:8453'.")] = None,
    max_price: Annotated[float | None, Field(default=None, ge=0, description="As in search_listings.")] = None,
    has_template: Annotated[bool | None, Field(default=None, description="Filter by whether output_schema is set.")] = None,
    stale: Annotated[bool | None, Field(default=None, description="Filter by the `stale` response field.")] = None,
    ctx: Context | None = None,
) -> CallToolResult:
    try:
        mcp_search_limiter.check_ip(_client_ip(ctx))
        counts = await list_facets(
            listing_type=listing_type,
            task_category=task_category,
            q=q,
            status=status,
            include_test=include_test,
            claimed=claimed,
            payment_network=payment_network,
            max_price=max_price,
            has_template=has_template,
            stale=stale,
        )
    except HTTPException as exc:
        return _error_result(exc, LIST_FACETS_TOOL_NAME)
    return CallToolResult(content=[TextContent(type="text", text=counts.model_dump_json())], isError=False)


_list_facets_tool.__name__ = LIST_FACETS_TOOL_NAME

GET_TEMPLATE_DESCRIPTION = (
    "A listing's full verification template (most useful for a verification_profile "
    "listing, but available on any listing that has one): output_schema, plus "
    "verification (optional cross-field rules/bounds/enforce_rules) and template_url "
    "when set - use it with the sibling verification service's POST /verify/schema to "
    "check a call's actual output against it (see that listing's next_actions for the "
    "exact suggested body). 404 no_template if the listing has no output_schema, "
    "not_found if the id does not exist. Free, no payment or account required."
)
_GET_TEMPLATE_ANNOTATIONS = ToolAnnotations(
    title="Get a listing's output template", readOnlyHint=True, destructiveHint=False,
    idempotentHint=True, openWorldHint=False,
)


async def _get_template_tool(
    listing_id: Annotated[str, Field(description="The listing's id (a UUID), e.g. from search_listings.")],
    ctx: Context | None = None,
) -> CallToolResult:
    try:
        mcp_search_limiter.check_ip(_client_ip(ctx))
        _validate_listing_id(listing_id)
        template = await get_template(listing_id)
    except HTTPException as exc:
        return _error_result(exc, GET_TEMPLATE_TOOL_NAME)
    return CallToolResult(content=[TextContent(type="text", text=template.model_dump_json())], isError=False)


_get_template_tool.__name__ = GET_TEMPLATE_TOOL_NAME


def _allowed_hosts() -> list[str]:
    hosts = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "testserver"]
    host = urlparse(os.getenv("SERVICE_BASE_URL", "")).hostname
    if host and host not in hosts:
        hosts += [host, f"{host}:*"]
    return hosts


class McpPathNormalizer:
    """Serve POST /mcp (no trailing slash) directly instead of answering with a
    307 redirect to /mcp/, which some MCP clients don't follow for POST. Same
    fix as the sibling verification service's own McpPathNormalizer."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == MCP_PATH:
            scope = {**scope, "path": MCP_PATH + "/", "raw_path": (MCP_PATH + "/").encode()}
        await self.app(scope, receive, send)


def create_server() -> FastMCP:
    server = FastMCP(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        stateless_http=True,       # every request is self-contained: no session affinity needed
        json_response=True,        # plain JSON replies, no SSE stream to hold open
        streamable_http_path="/",  # mounted at MCP_PATH by app/main.py
        # (Body size is capped for /mcp by MaxBodySizeMiddleware in app/main.py.)
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=_allowed_hosts()
        ),
        log_level="WARNING",
    )
    server.add_tool(_search_listings_tool, name=TOOL_NAME, description=TOOL_DESCRIPTION, annotations=_TOOL_ANNOTATIONS)
    server.add_tool(
        _get_listing_tool, name=GET_LISTING_TOOL_NAME, description=GET_LISTING_DESCRIPTION, annotations=_GET_LISTING_ANNOTATIONS
    )
    server.add_tool(
        _list_facets_tool, name=LIST_FACETS_TOOL_NAME, description=LIST_FACETS_DESCRIPTION, annotations=_LIST_FACETS_ANNOTATIONS
    )
    server.add_tool(
        _get_template_tool, name=GET_TEMPLATE_TOOL_NAME, description=GET_TEMPLATE_DESCRIPTION, annotations=_GET_TEMPLATE_ANNOTATIONS
    )
    return server
