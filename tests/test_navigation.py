"""Agent navigation: structured filters (payment_network, max_price, has_template,
stale) combinable with q; the facets endpoint/tool; next_actions on every listing;
output_schema / get_template; compact mode; and the three new MCP tools (get_listing,
list_facets, get_template)."""

import uuid

from eth_account import Account
from fastapi.testclient import TestClient

from app.core.score_client import VERIFICATION_SERVICE_URL
from app.main import app
from tests.helpers import assert_error, listing_payload

client = TestClient(app)

BASE = "eip155:8453"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_WALLET = "38Fmaf3MWTR6AWPWrtrdXoqn6iqfVcUBHMFRhiUAEjFb"


def _marker() -> str:
    return "nav-" + uuid.uuid4().hex[:8]


def _create(listing_type: str, **overrides) -> dict:
    owner = Account.create()
    payload = listing_payload(listing_type, owner.address, **overrides)
    response = client.post("/listings", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _ids(page: dict) -> list[str]:
    return [item["id"] for item in page["listings"]]


# ---- the stablecoin registry itself ----------------------------------------------------------


def test_is_usd_stablecoin_recognizes_known_pairs_case_insensitively() -> None:
    from app.core.stablecoins import is_usd_stablecoin

    assert is_usd_stablecoin(BASE, BASE_USDC) is True
    assert is_usd_stablecoin(BASE, BASE_USDC.upper()) is True  # case-insensitive asset match
    assert is_usd_stablecoin(SOLANA, SOLANA_USDC) is True
    assert is_usd_stablecoin(BASE, "0x4200000000000000000000000000000000000006") is False  # WETH
    assert is_usd_stablecoin(SOLANA, BASE_USDC) is False  # right asset, wrong network


# ---- structured filters, combinable with each other and with q ------------------------------


def test_payment_network_filter() -> None:
    marker = _marker()
    on_base = _create(marker, payment_options=[{"network": BASE, "asset": BASE_USDC, "pay_to": Account.create().address, "amount": "0.02", "unit": "per_call"}])
    on_solana = _create(marker, payment_options=[{"network": SOLANA, "asset": SOLANA_USDC, "pay_to": SOLANA_WALLET, "amount": "0.02", "unit": "per_call"}])

    page = client.get("/listings", params={"listing_type": marker, "payment_network": BASE}).json()
    ids = _ids(page)
    assert on_base["id"] in ids and on_solana["id"] not in ids


def test_max_price_filter() -> None:
    marker = _marker()
    cheap = _create(marker, payment_options=[{"network": BASE, "asset": BASE_USDC, "pay_to": Account.create().address, "amount": "0.01", "unit": "per_call"}])
    pricey = _create(marker, payment_options=[{"network": BASE, "asset": BASE_USDC, "pay_to": Account.create().address, "amount": "5.00", "unit": "per_call"}])

    page = client.get("/listings", params={"listing_type": marker, "max_price": 0.02}).json()
    ids = _ids(page)
    assert cheap["id"] in ids and pricey["id"] not in ids


def test_max_price_excludes_non_stablecoin_assets() -> None:
    marker = _marker()
    # WETH on Base - a tiny token amount, but not a recognized USD stablecoin, so no
    # max_price (a USD figure) should ever match it, however high.
    weth_listing = _create(
        marker,
        payment_options=[{"network": BASE, "asset": "0x4200000000000000000000000000000000000006", "pay_to": Account.create().address, "amount": "0.0001", "unit": "per_call"}],
    )
    page = client.get("/listings", params={"listing_type": marker, "max_price": 1_000_000}).json()
    assert weth_listing["id"] not in _ids(page)


def test_recognized_stablecoins_are_published_in_the_manifest() -> None:
    from app.core.stablecoins import stablecoin_pairs

    manifest = client.get("/.well-known/agent-card.json").json()
    params = manifest["capabilities"]["extensions"][0]["params"]
    published = params["search"]["stablecoins"]["pairs"]
    assert {(p["network"], p["asset"]) for p in published} == set(stablecoin_pairs())
    assert "USD" in params["search"]["structuredFilters"]


def test_payment_network_and_max_price_combine_on_the_same_payment_option() -> None:
    marker = _marker()
    # Cheap on Base, pricey on Solana - a listing with BOTH should only match the
    # combined filter if ONE option satisfies network AND price together.
    mixed = _create(
        marker,
        payment_options=[
            {"network": BASE, "asset": BASE_USDC, "pay_to": Account.create().address, "amount": "5.00", "unit": "per_call"},
            {"network": SOLANA, "asset": SOLANA_USDC, "pay_to": SOLANA_WALLET, "amount": "0.01", "unit": "per_call"},
        ],
    )
    matches_base_cheap = client.get(
        "/listings", params={"listing_type": marker, "payment_network": BASE, "max_price": 0.02}
    ).json()
    assert mixed["id"] not in _ids(matches_base_cheap)  # cheap option is on Solana, not Base

    matches_solana_cheap = client.get(
        "/listings", params={"listing_type": marker, "payment_network": SOLANA, "max_price": 0.02}
    ).json()
    assert mixed["id"] in _ids(matches_solana_cheap)


def test_invalid_payment_network_is_422() -> None:
    response = client.get("/listings", params={"payment_network": "not-a-caip2-id"})
    assert_error(response, 422, "validation_error")


def test_has_template_filter() -> None:
    marker = _marker()
    templated = _create(marker, output_schema={"type": "object"})
    plain = _create(marker)

    with_template = client.get("/listings", params={"listing_type": marker, "has_template": "true"}).json()
    assert templated["id"] in _ids(with_template) and plain["id"] not in _ids(with_template)

    without_template = client.get("/listings", params={"listing_type": marker, "has_template": "false"}).json()
    assert plain["id"] in _ids(without_template) and templated["id"] not in _ids(without_template)


def test_stale_filter_matches_the_stale_response_field() -> None:
    from datetime import datetime, timezone

    from app.core import db

    marker = _marker()
    listing = _create(marker)
    with db._connection() as conn:
        conn.execute("UPDATE listings SET missing_from_source_since = %s WHERE id = %s", (datetime.now(timezone.utc), listing["id"]))

    stale_page = client.get("/listings", params={"listing_type": marker, "stale": "true"}).json()
    assert listing["id"] in _ids(stale_page)
    fresh_page = client.get("/listings", params={"listing_type": marker, "stale": "false"}).json()
    assert listing["id"] not in _ids(fresh_page)


def test_structured_filters_combine_with_q() -> None:
    marker = _marker()
    match = _create(
        marker, name=f"Zephyr {marker}",
        payment_options=[{"network": BASE, "asset": BASE_USDC, "pay_to": Account.create().address, "amount": "0.01", "unit": "per_call"}],
    )
    wrong_network = _create(
        marker, name=f"Zephyr {marker}",
        payment_options=[{"network": SOLANA, "asset": SOLANA_USDC, "pay_to": SOLANA_WALLET, "amount": "0.01", "unit": "per_call"}],
    )
    page = client.get("/listings", params={"q": f"Zephyr {marker}", "payment_network": BASE}).json()
    ids = _ids(page)
    assert match["id"] in ids and wrong_network["id"] not in ids


# ---- facets -----------------------------------------------------------------------------------


def test_facets_endpoint_counts_by_dimension() -> None:
    marker = _marker()
    _create(marker, task_categories=["code review"], payment_options=[{"network": BASE, "asset": BASE_USDC, "pay_to": Account.create().address, "amount": "0.01", "unit": "per_call"}])
    _create(marker, task_categories=["translation"])

    facets = client.get("/listings/facets", params={"listing_type": marker}).json()
    assert facets["total"] == 2
    assert facets["by_task_category"]["code review"] == 1
    assert facets["by_task_category"]["translation"] == 1
    assert facets["by_listing_type"][marker] == 2
    assert facets["by_network"].get(BASE) == 1
    assert facets["by_source"]["none"] == 2


def test_facets_respects_the_same_filters_as_browse() -> None:
    marker = _marker()
    _create(marker, task_categories=["code review"])
    _create(marker, task_categories=["translation"])

    facets = client.get("/listings/facets", params={"listing_type": marker, "task_category": "code review"}).json()
    assert facets["total"] == 1
    assert facets["by_task_category"] == {"code review": 1}


def test_facets_mcp_tool_matches_rest() -> None:
    import asyncio

    from app.api.routes.listings import list_facets

    marker = _marker()
    _create(marker)
    rest = client.get("/listings/facets", params={"listing_type": marker}).json()
    tool = asyncio.run(list_facets(listing_type=marker))
    assert tool.total == rest["total"] == 1


# ---- next_actions -------------------------------------------------------------------------


def test_every_listing_carries_a_call_service_next_action() -> None:
    listing = _create(_marker())
    actions = {a["action"]: a for a in listing["next_actions"]}
    assert "call_service" in actions
    call = actions["call_service"]
    assert call["url"] == listing["endpoint_url"]
    assert "verify_output" not in actions  # no output_schema


def test_a_priced_listing_gets_price_and_networks_in_call_service() -> None:
    listing = _create(
        _marker(),
        payment_options=[{"network": BASE, "asset": BASE_USDC, "pay_to": Account.create().address, "amount": "0.05", "unit": "per_call"}],
    )
    call = next(a for a in listing["next_actions"] if a["action"] == "call_service")
    assert call["method"] == "POST"
    assert BASE in call["networks"]
    assert call["price"] is not None


def test_output_schema_adds_a_verify_output_next_action() -> None:
    listing = _create(_marker(), output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}})
    actions = {a["action"]: a for a in listing["next_actions"]}
    assert "verify_output" in actions
    verify = actions["verify_output"]
    assert verify["url"] == f"{VERIFICATION_SERVICE_URL}/verify/schema"
    assert verify["method"] == "POST"
    # Field names match the verifier's own POST /verify/schema and verify_schema MCP
    # tool exactly - confirmed against its live OpenAPI spec, not guessed.
    assert set(verify["body"]) == {"task_id", "expected_schema", "submitted_output"}
    assert verify["body"]["expected_schema"] == listing["output_schema"]


def test_verification_folds_rules_and_enforce_rules_into_the_verify_output_body() -> None:
    schema = {"type": "object"}
    verification = {
        "rules": [{"type": "unique", "field": "items[].id"}],
        "bounds": {"total": {"min": 0}},
        "enforce_rules": True,
    }
    listing = _create(_marker(), output_schema=schema, verification=verification)
    verify = next(a for a in listing["next_actions"] if a["action"] == "verify_output")
    assert verify["body"]["expected_schema"] == schema
    assert verify["body"]["rules"] == verification["rules"]
    assert verify["body"]["bounds"] == verification["bounds"]
    assert verify["body"]["enforce_rules"] is True
    assert "run its cross-field rules" in verify["description"].lower() or "not just the schema" in verify["description"].lower()


def test_verification_without_output_schema_does_not_add_a_next_action() -> None:
    # verification only makes sense alongside output_schema (what are the rules checking?).
    listing = _create(_marker(), verification={"rules": [], "bounds": {}, "enforce_rules": True})
    assert not any(a["action"] == "verify_output" for a in listing["next_actions"])


# ---- output_schema: creation, validation, patching -----------------------------------------


def test_output_schema_round_trips() -> None:
    schema = {"type": "object", "required": ["result"], "properties": {"result": {"type": "string"}}}
    listing = _create(_marker(), output_schema=schema)
    assert listing["output_schema"] == schema
    assert client.get(f"/listings/{listing['id']}").json()["output_schema"] == schema


def test_output_schema_must_be_a_json_object() -> None:
    owner = Account.create()
    payload = listing_payload("offering", owner.address, output_schema=["not", "an", "object"])
    response = client.post("/listings", json=payload)
    assert response.status_code == 422


def test_output_schema_oversized_is_rejected() -> None:
    owner = Account.create()
    huge = {"description": "x" * 25_000}
    payload = listing_payload("offering", owner.address, output_schema=huge)
    response = client.post("/listings", json=payload)
    assert response.status_code == 422


def test_verification_round_trips_and_must_be_a_json_object() -> None:
    verification = {"rules": [{"type": "unique", "field": "a"}], "bounds": {}, "enforce_rules": False}
    listing = _create(_marker(), verification=verification)
    assert listing["verification"] == verification

    owner = Account.create()
    bad = listing_payload("offering", owner.address, verification=["not", "an", "object"])
    assert client.post("/listings", json=bad).status_code == 422


def test_template_url_round_trips_and_must_be_http_or_https() -> None:
    listing = _create(_marker(), template_url="https://example.com/templates/t.json")
    assert listing["template_url"] == "https://example.com/templates/t.json"

    owner = Account.create()
    bad = listing_payload("offering", owner.address, template_url="ftp://example.com/t.json")
    assert client.post("/listings", json=bad).status_code == 422


def test_output_schema_can_be_set_and_cleared_via_patch() -> None:
    from tests.helpers import wallet_auth_header

    owner = Account.create()
    listing = client.post("/listings", json=listing_payload("offering", owner.address)).json()
    assert listing["output_schema"] is None

    patch = {"output_schema": {"type": "object"}}
    header = wallet_auth_header(owner, action="update-listing", listing_id=listing["id"], body=patch)
    patched = client.patch(f"/listings/{listing['id']}", json=patch, headers={"X-Wallet-Auth": header})
    assert patched.status_code == 200 and patched.json()["output_schema"] == {"type": "object"}

    clear_patch = {"output_schema": None}
    header2 = wallet_auth_header(owner, action="update-listing", listing_id=listing["id"], body=clear_patch)
    cleared = client.patch(f"/listings/{listing['id']}", json=clear_patch, headers={"X-Wallet-Auth": header2})
    assert cleared.status_code == 200 and cleared.json()["output_schema"] is None


# ---- get_template -------------------------------------------------------------------------


def test_get_template_returns_the_schema() -> None:
    schema = {"type": "object"}
    listing = _create(_marker(), output_schema=schema)
    response = client.get(f"/listings/{listing['id']}/template")
    assert response.status_code == 200
    assert response.json() == {
        "listing_id": listing["id"], "output_schema": schema, "verification": None, "template_url": None,
    }


def test_get_template_includes_verification_and_template_url_when_set() -> None:
    schema = {"type": "object"}
    verification = {"rules": [{"type": "unique", "field": "items[].id"}], "bounds": {"total": {"min": 0}}, "enforce_rules": True}
    listing = _create(
        _marker(), output_schema=schema, verification=verification, template_url="https://example.com/t.json"
    )
    response = client.get(f"/listings/{listing['id']}/template").json()
    assert response["verification"] == verification
    assert response["template_url"] == "https://example.com/t.json"


def test_get_template_404s_with_no_template() -> None:
    listing = _create(_marker())
    response = client.get(f"/listings/{listing['id']}/template")
    assert_error(response, 404, "no_template")


def test_get_template_404s_for_a_nonexistent_listing() -> None:
    response = client.get(f"/listings/{uuid.uuid4()}/template")
    assert_error(response, 404, "not_found")


# ---- compact mode -------------------------------------------------------------------------


def test_compact_mode_returns_exactly_the_documented_fields() -> None:
    marker = _marker()
    listing = _create(
        marker,
        payment_options=[{"network": BASE, "asset": BASE_USDC, "pay_to": Account.create().address, "amount": "0.02", "unit": "per_call"}],
    )
    page = client.get("/listings", params={"listing_type": marker, "compact": "true"}).json()
    assert len(page["listings"]) == 1
    item = page["listings"][0]
    assert set(item) == {"id", "name", "endpoint_url", "price", "networks", "task_categories", "claimed"}
    assert item["id"] == listing["id"]
    assert BASE in item["networks"]


def test_compact_mode_mcp_tool_matches_rest() -> None:
    import asyncio

    from app.api.routes.listings import search_listings

    marker = _marker()
    _create(marker)
    rest = client.get("/listings", params={"listing_type": marker, "compact": "true"}).json()
    tool_page = asyncio.run(search_listings(listing_type=marker, compact=True))
    assert [i.id for i in tool_page.listings] == [i["id"] for i in rest["listings"]]
    assert set(tool_page.listings[0].model_dump()) == {"id", "name", "endpoint_url", "price", "networks", "task_categories", "claimed"}


# ---- MCP tools: get_listing, list_facets, get_template ---------------------------------------


def test_mcp_get_listing_matches_rest() -> None:
    import asyncio

    from app.api.routes.listings import get_listing as shared_get_listing

    listing = _create(_marker())
    rest = client.get(f"/listings/{listing['id']}").json()
    tool_result = asyncio.run(shared_get_listing(listing["id"]))
    assert tool_result.id == rest["id"] == listing["id"]
    assert tool_result.name == rest["name"]


def test_mcp_get_template_matches_rest() -> None:
    import asyncio

    from app.api.routes.listings import get_template as shared_get_template

    listing = _create(_marker(), output_schema={"type": "object"})
    rest = client.get(f"/listings/{listing['id']}/template").json()
    tool_result = asyncio.run(shared_get_template(listing["id"]))
    assert tool_result.listing_id == rest["listing_id"]
    assert tool_result.output_schema == rest["output_schema"]
