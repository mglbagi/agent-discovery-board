"""End-to-end tests of the search_listings MCP tool, over a real MCP client/server
transport (streamable HTTP) against a real, temporarily-running instance of this
service - not a monkeypatched stand-in of the tool function. Confirms it returns
results identical to GET /listings for the same filters (they share one function,
app.api.routes.listings.search_listings), that it's free (no payment mechanics
anywhere in the exchange), and that its own rate limiter works.
"""

import asyncio
import json

import httpx
import pytest
import pytest_asyncio
import uvicorn
from eth_account import Account
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.core.rate_limit import mcp_search_limiter
from app.main import app
from tests.helpers import assert_error_body, listing_payload, wallet_auth_header

# The app's lifespan enters mcp_server.session_manager.run() (app/main.py), and the
# MCP SDK only allows that exactly once per FastMCP instance - app.main.mcp_server is
# a module-level singleton, so the live server can only be started (and its lifespan
# entered) ONCE per test session, not once per test. Both the fixture and every test
# that uses it therefore share one module-scoped event loop.
pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live_server():
    """A real uvicorn server for `app`, bound to an OS-assigned port, shared by every
    test in this module. Needed (over FastAPI's in-process TestClient) because the
    MCP tool reads the caller's IP off a real Starlette Request (app/mcp_server.py's
    _client_ip), which only exists on an actual HTTP transport."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(500):  # 5s bound: a failed startup must fail loud, never hang forever
        if server.started:
            break
        await asyncio.sleep(0.01)
    else:
        raise RuntimeError("Test server did not start within 5 seconds - check for a startup error above.")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.fixture(autouse=True)
def _isolated_mcp_search_limiter():
    mcp_search_limiter.reset()
    yield
    mcp_search_limiter.reset()


async def _create_via_rest(base_url: str, listing_type: str = "offering", owner=None, **overrides) -> dict:
    owner = owner or Account.create()
    payload = listing_payload(listing_type, owner.address, **overrides)
    async with httpx.AsyncClient() as http:
        response = await http.post(f"{base_url}/listings", json=payload)
        response.raise_for_status()
        return response.json()


async def _call_tool(base_url: str, name: str, arguments: dict):
    async with streamablehttp_client(f"{base_url}/mcp/") as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        return await session.call_tool(name, arguments)


def _body(result) -> dict:
    return json.loads(result.content[0].text)


async def test_tool_is_discoverable_with_no_payment_mechanics(live_server) -> None:
    async with streamablehttp_client(f"{live_server}/mcp/") as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        assert [t.name for t in tools] == ["search_listings"]
        tool = tools[0]
        # No x402/pricing vocabulary anywhere in the tool's own description - unlike
        # the verification service's paid MCP tool, this one must read as free, and
        # say so explicitly rather than just omitting pricing.
        assert "x402" not in tool.description.lower()
        assert "usdc" not in tool.description.lower()
        assert "no payment" in tool.description.lower()


async def test_mcp_results_match_rest_results_for_the_same_filter(live_server) -> None:
    marker = "mcp-parity-" + Account.create().address[-8:]
    created = await _create_via_rest(live_server, name=f"Reviewer {marker}", task_categories=["code review"])

    async with httpx.AsyncClient() as http:
        rest = (await http.get(f"{live_server}/listings", params={"q": marker})).json()

    mcp_result = await _call_tool(live_server, "search_listings", {"q": marker})
    mcp_body = _body(mcp_result)

    assert mcp_result.isError is False
    assert mcp_body["total"] == rest["total"] == 1
    assert mcp_body["listings"][0]["id"] == rest["listings"][0]["id"] == created["id"]


async def test_filter_by_listing_type_via_mcp(live_server) -> None:
    marker = "mcp-type-" + Account.create().address[-8:]
    offering = await _create_via_rest(live_server, name=f"Offer {marker}")
    request_listing = await _create_via_rest(live_server, "request", name=f"Ask {marker}")

    result = await _call_tool(live_server, "search_listings", {"listing_type": "offering", "q": marker})
    ids = [item["id"] for item in _body(result)["listings"]]
    assert offering["id"] in ids
    assert request_listing["id"] not in ids


async def test_filter_by_task_category_via_mcp(live_server) -> None:
    marker = "mcp-cat-" + Account.create().address[-8:]
    match = await _create_via_rest(live_server, name=f"Cat {marker}", task_categories=["translation"])
    other = await _create_via_rest(live_server, name=f"Cat {marker}", task_categories=["scheduling"])

    result = await _call_tool(live_server, "search_listings", {"task_category": ["translation"], "q": marker})
    ids = [item["id"] for item in _body(result)["listings"]]
    assert match["id"] in ids
    assert other["id"] not in ids


async def test_pagination_via_mcp(live_server) -> None:
    marker = "mcp-page-" + Account.create().address[-8:]
    created_ids = [(await _create_via_rest(live_server, name=f"Page {marker} #{i}"))["id"] for i in range(3)]

    page1 = _body(await _call_tool(live_server, "search_listings", {"q": marker, "limit": 2, "offset": 0}))
    page2 = _body(await _call_tool(live_server, "search_listings", {"q": marker, "limit": 2, "offset": 2}))

    assert page1["total"] == 3
    assert len(page1["listings"]) == 2
    assert len(page2["listings"]) == 1
    seen = {i["id"] for i in page1["listings"]} | {i["id"] for i in page2["listings"]}
    assert seen == set(created_ids)


async def test_unknown_task_category_is_a_tool_error_not_a_crash(live_server) -> None:
    result = await _call_tool(live_server, "search_listings", {"task_category": ["not-a-real-category"]})
    assert result.isError is True
    body = _body(result)
    assert body["status"] == 422  # the tool's original keys are unchanged...
    assert "not-a-real-category" in body["error"]
    assert_error_body(body, "invalid_task_category")  # ...and the machine-readable ones are added
    assert body["next_actions"][0]["method"] == "MCP_TOOL" and body["next_actions"][0]["path"] == "search_listings"


async def test_deactivated_listings_excluded_by_default_via_mcp(live_server) -> None:
    owner = Account.create()
    marker = "mcp-deact-" + owner.address[-8:]
    created = await _create_via_rest(live_server, owner=owner, name=f"Deactivate {marker}")

    header = wallet_auth_header(owner, action="delete-listing", listing_id=created["id"], body=None)
    async with httpx.AsyncClient() as http:
        r = await http.delete(f"{live_server}/listings/{created['id']}", headers={"X-Wallet-Auth": header})
        r.raise_for_status()

    active_only = _body(await _call_tool(live_server, "search_listings", {"q": marker}))
    assert created["id"] not in [i["id"] for i in active_only["listings"]]

    with_inactive = _body(await _call_tool(live_server, "search_listings", {"q": marker, "status": "inactive"}))
    assert created["id"] in [i["id"] for i in with_inactive["listings"]]


async def test_rest_get_listings_is_unaffected_by_the_mcp_tool_existing(live_server) -> None:
    marker = "mcp-additive-" + Account.create().address[-8:]
    created = await _create_via_rest(live_server, name=f"Still REST {marker}")
    async with httpx.AsyncClient() as http:
        response = await http.get(f"{live_server}/listings", params={"q": marker})
    assert response.status_code == 200
    assert created["id"] in [i["id"] for i in response.json()["listings"]]


async def test_search_listings_tool_is_rate_limited(live_server) -> None:
    mcp_search_limiter.max_requests = 2
    mcp_search_limiter.window_seconds = 60.0
    mcp_search_limiter.reset()
    try:
        for _ in range(2):
            result = await _call_tool(live_server, "search_listings", {})
            assert result.isError is False

        limited = await _call_tool(live_server, "search_listings", {})
        assert limited.isError is True
        body = _body(limited)
        assert body["status"] == 429
        assert body["retryable"] is True
        assert_error_body(body, "rate_limited")
        assert isinstance(body["retry_after"], int) and body["retry_after"] >= 1
        assert str(body["retry_after"]) in body["next_actions"][0]["description"]
    finally:
        mcp_search_limiter.max_requests = 30
        mcp_search_limiter.window_seconds = 60.0
        mcp_search_limiter.reset()


async def test_results_carry_freshness_fields_and_are_newest_activity_first(live_server) -> None:
    marker = "mcp-fresh-" + Account.create().address[-8:]
    old = await _create_via_rest(live_server, name=f"Old {marker}")
    new = await _create_via_rest(live_server, name=f"New {marker}")
    from tests.helpers import set_listing_times

    set_listing_times(old["id"], created_days_ago=90, updated_days_ago=90, last_seen_hours_ago=None)

    body = _body(await _call_tool(live_server, "search_listings", {"q": marker}))
    assert [i["id"] for i in body["listings"]] == [new["id"], old["id"]]
    by_id = {i["id"]: i for i in body["listings"]}
    assert by_id[old["id"]]["stale"] is True and by_id[new["id"]]["stale"] is False
    assert {"last_seen_at", "last_activity_at", "payment_options"} <= set(by_id[new["id"]])
    assert body["next_cursor"] is None


async def test_cursor_pagination_via_mcp_matches_rest(live_server) -> None:
    marker = "mcp-cursor-" + Account.create().address[-8:]
    created = {(await _create_via_rest(live_server, name=f"C {marker} {i}"))["id"] for i in range(5)}

    seen, cursor = [], None
    for _ in range(10):
        args = {"q": marker, "limit": 2, **({"cursor": cursor} if cursor else {})}
        body = _body(await _call_tool(live_server, "search_listings", args))
        seen += [i["id"] for i in body["listings"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) and set(seen) == created

    async with httpx.AsyncClient() as http:
        rest = (await http.get(f"{live_server}/listings", params={"q": marker, "limit": 100})).json()
    assert seen == [i["id"] for i in rest["listings"]]


async def test_a_bad_cursor_via_mcp_is_a_coded_error(live_server) -> None:
    result = await _call_tool(live_server, "search_listings", {"cursor": "not-a-cursor"})
    assert result.isError is True
    body = _body(result)
    assert_error_body(body, "invalid_cursor")
    assert body["next_actions"][0]["description"].startswith("Call again without a cursor")
