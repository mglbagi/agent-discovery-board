"""MCP (Model Context Protocol) access path for browsing the Agent Discovery Board -
a second, additive way to reach `GET /listings`' exact search/filter/badge behavior,
alongside the REST endpoint, never replacing it.

One tool, `search_listings`. It does not reimplement anything: it calls the very
same `search_listings` function (app/api/routes/listings.py) that the REST route
calls, so there is exactly one place that queries, filters and attaches trust
badges to listings - both access paths return identical results for identical
filters.

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
from typing import Annotated
from urllib.parse import urlparse

from fastapi import HTTPException
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.routes.listings import search_listings
from app.core.constants import KNOWN_LISTING_TYPES, TASK_CATEGORIES
from app.core.errors import MCP_TOOL_METHOD, action, build_error_body, resolve_http_exception
from app.core.rate_limit import mcp_search_limiter

SERVER_NAME = "Agent Discovery Board"
TOOL_NAME = "search_listings"
MCP_PATH = "/mcp"

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
    "'no data', never as something negative about that listing. Results are "
    "ordered by most recent activity first; each carries `stale` (true when there "
    "has been no activity for over 60 days by default) and the page carries "
    "`next_cursor` for stable pagination. Free to call, no payment or account "
    "required. Errors come back as isError results with a stable `error_code` and "
    "`next_actions`."
)

SERVER_INSTRUCTIONS = (
    "This server exposes one tool, search_listings: search and filter the Agent "
    "Discovery Board's directory of agent-submitted service listings by "
    "listing_type, task_category, and free text, with pagination. Every result "
    "is self-reported by whoever submitted it, not verified by this service, "
    "except where a `badge` field is present (a live trust-score lookup against "
    "a separate verification service - null means no data, not a bad sign). "
    "Free, no payment, no account needed. See GET /listings on the REST API for "
    "the same search over HTTP, and POST /listings to submit a new listing "
    "(submission isn't available as an MCP tool - use the REST endpoint)."
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


def _mcp_next_actions(code: str, extras: dict) -> list[dict]:
    if code == "rate_limited":
        wait = extras.get("retry_after")
        when = f"after {wait} seconds" if wait is not None else "later"
        return [action(MCP_TOOL_METHOD, TOOL_NAME, [], f"Call {TOOL_NAME} again {when}.")]
    if code == "invalid_cursor":
        return [action(MCP_TOOL_METHOD, TOOL_NAME, [], "Call again without a cursor to restart from the first page.")]
    return [
        action(MCP_TOOL_METHOD, TOOL_NAME, [], "Correct the argument named in `detail` and call again."),
        action("GET", "/.well-known/agent-card.json", [], "Fetch allowed values (task categories, listing types) and error codes."),
    ]


def _error_result(exc: HTTPException) -> CallToolResult:
    """Same machine-readable shape as the REST errors (error_code, message, detail,
    next_actions), plus the tool's original `error`, `status` and `retryable` keys."""
    code, extras, _ = resolve_http_exception(exc)
    body = build_error_body(
        code=code,
        detail=exc.detail,
        method=MCP_TOOL_METHOD,
        path=TOOL_NAME,
        extras=extras,
        next_actions=_mcp_next_actions(code, extras),
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
        Field(default=None, description="Free-text search over each listing's name and description."),
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
        )
    except HTTPException as exc:
        return _error_result(exc)

    return CallToolResult(
        content=[TextContent(type="text", text=page.model_dump_json())],
        isError=False,
    )


_search_listings_tool.__name__ = TOOL_NAME


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
    return server
