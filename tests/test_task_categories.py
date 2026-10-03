"""The fixed task_category set grew by five browse categories for the imported verification
profiles. Additive only: the original eleven names are unchanged, and the full set appears
everywhere the board lists categories (filters, facets, manifest, llms.txt, MCP tools)."""

import asyncio
import uuid

from eth_account import Account
from fastapi.testclient import TestClient

from app.core.constants import TASK_CATEGORIES
from app.main import app
from tests.helpers import assert_error, listing_payload

client = TestClient(app)

ORIGINAL = (
    "data extraction", "summarization", "content generation", "code generation", "code review",
    "research/search", "translation", "image generation", "data validation", "scheduling", "other",
)
NEW = (
    "finance and tax", "crypto and blockchain data", "security and compliance",
    "commerce and shopping", "media generation",
)


def _marker() -> str:
    return "cat-" + uuid.uuid4().hex[:8]


def _create(listing_type: str, **overrides) -> dict:
    payload = listing_payload(listing_type, Account.create().address, **overrides)
    response = client.post("/listings", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_the_original_categories_are_all_still_valid_and_the_new_ones_are_added() -> None:
    assert set(ORIGINAL) <= set(TASK_CATEGORIES)
    assert set(NEW) <= set(TASK_CATEGORIES)
    assert len(TASK_CATEGORIES) == len(set(TASK_CATEGORIES)) == len(ORIGINAL) + len(NEW)


def test_new_names_are_safe_to_put_in_a_query_string() -> None:
    # An unencoded "&" would split the task_category parameter in two.
    for name in TASK_CATEGORIES:
        assert "&" not in name and "?" not in name and "#" not in name and "+" not in name


def test_every_category_can_be_stored_and_filtered_on() -> None:
    marker = _marker()
    created = {c: _create(marker, task_categories=[c]) for c in TASK_CATEGORIES}
    for category, listing in created.items():
        page = client.get("/listings", params={"listing_type": marker, "task_category": category}).json()
        assert [item["id"] for item in page["listings"]] == [listing["id"]], category
        assert page["listings"][0]["task_categories"] == [category]


def test_a_listing_can_carry_new_and_old_categories_together() -> None:
    marker = _marker()
    both = _create(marker, task_categories=["data validation", "finance and tax", "media generation"])
    for category in ("data validation", "finance and tax", "media generation"):
        page = client.get("/listings", params={"listing_type": marker, "task_category": category}).json()
        assert [item["id"] for item in page["listings"]] == [both["id"]]
    # repeating the parameter matches any overlap
    page = client.get(
        "/listings", params={"listing_type": marker, "task_category": ["security and compliance", "finance and tax"]}
    ).json()
    assert page["total"] == 1


def test_a_category_name_is_found_by_free_text_search() -> None:
    marker = _marker()
    listing = _create(marker, name=f"Ledger helper {marker}", description="Checks outputs.", task_categories=["finance and tax"])
    page = client.get("/listings", params={"q": "tax", "listing_type": marker}).json()
    assert listing["id"] in [item["id"] for item in page["listings"]]


def test_an_unknown_category_is_still_rejected_and_the_error_lists_the_full_set() -> None:
    response = client.get("/listings", params={"task_category": "finance & tax"})
    message = assert_error(response, 422, "invalid_task_category")["message"]
    for category in TASK_CATEGORIES:
        assert category in message
    bad = listing_payload("offering", Account.create().address, task_categories=["finance & tax"])
    assert client.post("/listings", json=bad).status_code == 422


def test_facets_list_every_category_with_zero_for_the_empty_ones() -> None:
    marker = _marker()
    _create(marker, task_categories=["finance and tax"])
    _create(marker, task_categories=["finance and tax", "data validation"])
    facets = client.get("/listings/facets", params={"listing_type": marker}).json()
    assert set(facets["by_task_category"]) == set(TASK_CATEGORIES)
    assert facets["by_task_category"]["finance and tax"] == 2
    assert facets["by_task_category"]["data validation"] == 1
    assert facets["by_task_category"]["media generation"] == 0
    assert facets["total"] == 2  # the zero entries don't change the total


def test_facets_zero_fill_also_applies_when_nothing_matches() -> None:
    facets = client.get("/listings/facets", params={"q": "zzzqqqxxnomatch" + uuid.uuid4().hex}).json()
    assert facets["total"] == 0
    assert facets["by_task_category"] == {c: 0 for c in TASK_CATEGORIES}


def test_manifest_agent_card_and_llms_txt_list_the_full_set() -> None:
    card = client.get("/.well-known/agent-card.json").json()
    assert card["capabilities"]["extensions"][0]["params"]["taskCategories"] == list(TASK_CATEGORIES)
    llms = client.get("/llms.txt").text
    for category in TASK_CATEGORIES:
        assert category in llms


def test_openapi_filter_and_create_schema_pick_up_the_new_names() -> None:
    schema = client.get("/openapi.json").text
    for category in NEW:
        assert category in schema


def test_mcp_tool_descriptions_and_filter_params_list_the_full_set() -> None:
    from app import mcp_server

    async def tools():
        return await mcp_server.create_server().list_tools()

    listed = {t.name: t for t in asyncio.run(tools())}
    for name in ("search_listings", "list_facets"):
        for category in TASK_CATEGORIES:
            assert category in listed[name].description, (name, category)
        param = listed[name].inputSchema["properties"]["task_category"]
        assert all(category in str(param) for category in TASK_CATEGORIES), name
