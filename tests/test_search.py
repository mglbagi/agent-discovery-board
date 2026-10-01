"""Postgres full-text search (app/core/db.py's list_listings): natural-language
queries via websearch_to_tsquery, with stemming, across name/description/
task_categories (name weighted above description, above task_categories); a pg_trgm
word-similarity fallback for typos/partial words when full-text finds nothing; stable
cursor pagination through either mode; and query logging.

Every test below scopes its own listings with a unique listing_type marker, combined
with `q`, so results are never polluted by other tests' or production-like data in the
same shared test database - see tests/test_activity.py for why listing_type (not q
itself) is the isolation key of choice.
"""

import uuid

from eth_account import Account
from fastapi.testclient import TestClient

from app.core import db
from app.main import app
from tests.helpers import assert_error, listing_payload

client = TestClient(app)


def _create(listing_type: str, **overrides) -> dict:
    owner = Account.create()
    payload = listing_payload(listing_type, owner.address, **overrides)
    response = client.post("/listings", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _search(q: str, listing_type: str, **params) -> dict:
    response = client.get("/listings", params={"q": q, "listing_type": listing_type, **params})
    assert response.status_code == 200, response.text
    return response.json()


def _ids(page: dict) -> list[str]:
    return [item["id"] for item in page["listings"]]


# ---- the reported bug: natural-language queries against realistic listing text -------------


VERIFIER_DESCRIPTION = (
    "Verify an agent's output before you pay. Sellers: check your own output before you submit it, and deliver "
    "it with a signed receipt as evidence of verification. Checks machine-checkable requirements (structure, "
    "integrity, formats, ranges, rules), not whether the content is correct. POST /verify/schema checks any JSON "
    "value against a caller-supplied JSON Schema (draft-04 through 2020-12) and returns pass/fail, every "
    "violation (not just the first), template-based fix hints for a verify-repair-verify loop, and the share of "
    "checks passed. Every response carries an Ed25519-signed receipt that binds the result to the exact output, "
    "schema and rules that were checked, so it can be verified independently of the service."
)


def _make_verifier_like_listing(marker: str) -> dict:
    return _create(marker, name="Agent Output Verifier", description=VERIFIER_DESCRIPTION)


def test_the_reported_query_now_finds_the_verifier_listing() -> None:
    marker = "search-" + uuid.uuid4().hex[:8]
    listing = _make_verifier_like_listing(marker)
    page = _search("verify agent output before paying", marker)
    assert _ids(page) == [listing["id"]]


def test_a_shorter_prefix_of_the_same_query_still_matches() -> None:
    marker = "search-" + uuid.uuid4().hex[:8]
    listing = _make_verifier_like_listing(marker)
    assert _ids(_search("verify agent output", marker)) == [listing["id"]]


def test_json_schema_validation_matches_via_fallback_when_the_exact_word_is_absent() -> None:
    # The listing's text has "JSON Schema" but never the word "validation" - an AND of
    # all three words finds nothing in full-text, so this specifically exercises the
    # trigram fallback, not just full-text.
    marker = "search-" + uuid.uuid4().hex[:8]
    listing = _make_verifier_like_listing(marker)
    page = _search("json schema validation", marker)
    assert listing["id"] in _ids(page)


def test_invoice_matches_an_invoice_listing_and_not_the_verifier() -> None:
    marker = "search-" + uuid.uuid4().hex[:8]
    _make_verifier_like_listing(marker)
    invoice_listing = _create(
        marker, name="Invoice Processor", description="Extracts line items and totals from invoice PDFs."
    )
    page = _search("invoice", marker)
    assert _ids(page) == [invoice_listing["id"]]


def test_signed_receipt_matches_the_verifier() -> None:
    marker = "search-" + uuid.uuid4().hex[:8]
    listing = _make_verifier_like_listing(marker)
    assert _ids(_search("signed receipt", marker)) == [listing["id"]]


def test_a_misspelling_still_finds_the_listing_via_the_trigram_fallback() -> None:
    marker = "search-" + uuid.uuid4().hex[:8]
    listing = _make_verifier_like_listing(marker)
    page = _search("verfy", marker)
    assert listing["id"] in _ids(page)


def test_a_query_with_no_relevant_listing_returns_nothing() -> None:
    marker = "search-" + uuid.uuid4().hex[:8]
    _make_verifier_like_listing(marker)
    page = _search("zzzqqq completely unrelated gibberish about spaceships", marker)
    assert page["listings"] == [] and page["total"] == 0


# ---- stemming -------------------------------------------------------------------------------


def test_verify_matches_a_listing_that_only_says_verification() -> None:
    marker = "stem-" + uuid.uuid4().hex[:8]
    listing = _create(marker, name="Doc Checker", description="Independent verification of agent output quality.")
    assert listing["id"] in _ids(_search("verify agent output", marker))


def test_paying_matches_a_listing_that_only_says_pay() -> None:
    marker = "stem-" + uuid.uuid4().hex[:8]
    listing = _create(marker, name="Toll Booth", description="You pay a small fee per call.")
    assert listing["id"] in _ids(_search("paying a fee", marker))


# ---- search across name, description and task_categories, case-insensitively ----------------


def test_matches_task_categories_not_just_name_and_description() -> None:
    marker = "field-" + uuid.uuid4().hex[:8]
    listing = _create(marker, name="Generic Agent", description="Does generic things.", task_categories=["translation"])
    other = _create(marker, name="Other Agent", description="Also generic.", task_categories=["scheduling"])
    page = _search("translation", marker)
    assert _ids(page) == [listing["id"]]
    assert other["id"] not in _ids(page)


def test_search_is_case_insensitive() -> None:
    marker = "case-" + uuid.uuid4().hex[:8]
    listing = _create(marker, name="Loud Name ZEPHYR")
    assert listing["id"] in _ids(_search("zephyr", marker))


# ---- ranking: name matches above description-only matches, task_categories lowest -----------


def test_a_name_match_outranks_a_description_only_match() -> None:
    marker = "rank-" + uuid.uuid4().hex[:8]
    in_name = _create(marker, name="Zephyr Widget", description="Nothing special here.")
    in_description = _create(marker, name="Other Thing", description="This mentions zephyr deep in the text.")
    page = _search("zephyr", marker)
    assert _ids(page) == [in_name["id"], in_description["id"]]


def test_a_description_match_outranks_a_task_category_only_match() -> None:
    marker = "rank-" + uuid.uuid4().hex[:8]
    in_description = _create(marker, name="Alpha", description="This is about translation work.")
    in_category = _create(marker, name="Beta", description="Nothing relevant here.", task_categories=["translation"])
    page = _search("translation", marker)
    assert _ids(page) == [in_description["id"], in_category["id"]]


# ---- cursor pagination stays stable through ranked and fallback search ----------------------


def test_cursor_pagination_over_ranked_results_visits_everything_once() -> None:
    marker = "rankpage-" + uuid.uuid4().hex[:8]
    created = [_create(marker, name=f"Zephyr unit {i}") for i in range(7)]

    seen, cursor = [], None
    for _ in range(20):
        page = _search("zephyr", marker, limit=3, **({"cursor": cursor} if cursor else {}))
        seen += _ids(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(c["id"] for c in created)
    assert len(seen) == len(set(seen))
    assert seen == _ids(_search("zephyr", marker, limit=100))  # same order as one big page


def test_cursor_pagination_over_fallback_results_visits_everything_once() -> None:
    marker = "fallpage-" + uuid.uuid4().hex[:8]
    created = [_create(marker, name=f"Zphyrr unit {i}", description="says zphyrr, not the real word") for i in range(5)]

    seen, cursor = [], None
    for _ in range(20):
        page = _search("zephyr", marker, limit=2, **({"cursor": cursor} if cursor else {}))
        seen += _ids(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(c["id"] for c in created)
    assert len(seen) == len(set(seen))


def test_a_cursor_from_one_query_is_rejected_for_a_different_query() -> None:
    marker = "cursormix-" + uuid.uuid4().hex[:8]
    for i in range(3):
        _create(marker, name=f"Zephyr unit {i}")
    cursor = _search("zephyr", marker, limit=1)["next_cursor"]
    assert cursor is not None
    response = client.get("/listings", params={"q": "something else entirely", "listing_type": marker, "cursor": cursor})
    assert_error(response, 422, "invalid_cursor")


def test_a_search_cursor_is_rejected_for_a_plain_browse() -> None:
    marker = "cursormix-" + uuid.uuid4().hex[:8]
    for i in range(3):
        _create(marker, name=f"Zephyr unit {i}")
    cursor = _search("zephyr", marker, limit=1)["next_cursor"]
    response = client.get("/listings", params={"listing_type": marker, "cursor": cursor})
    assert_error(response, 422, "invalid_cursor")


def test_a_plain_browse_cursor_is_rejected_for_a_search() -> None:
    marker = "cursormix-" + uuid.uuid4().hex[:8]
    for i in range(3):
        _create(marker, name=f"Listing {i}")
    cursor = client.get("/listings", params={"listing_type": marker, "limit": 1}).json()["next_cursor"]
    assert cursor is not None
    response = client.get("/listings", params={"q": "listing", "listing_type": marker, "cursor": cursor})
    assert_error(response, 422, "invalid_cursor")


# ---- query logging: text, result count and timestamp only, nothing else ---------------------


def test_a_search_is_logged_with_its_result_count() -> None:
    marker = "log-" + uuid.uuid4().hex[:8]
    query = f"findme {marker}"
    _create(marker, name=f"Findme {marker}")
    _search(query, marker)

    with db._connection() as conn:
        row = conn.execute(
            "SELECT result_count, searched_at FROM search_log WHERE query = %s ORDER BY id DESC LIMIT 1", (query,)
        ).fetchone()
    assert row is not None
    assert row["result_count"] == 1
    assert row["searched_at"] is not None


def test_a_plain_browse_with_no_q_is_not_logged() -> None:
    marker = "log-" + uuid.uuid4().hex[:8]
    _create(marker, name="Anything")
    before = _log_count(marker)
    client.get("/listings", params={"listing_type": marker})
    assert _log_count(marker) == before


def _log_count(marker: str) -> int:
    with db._connection() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM search_log WHERE query LIKE %s", (f"%{marker}%",)).fetchone()["n"]


# ---- MCP parity: the tool shares this exact logic, not a reimplementation -------------------


def test_mcp_search_listings_ranks_the_same_way_as_rest() -> None:
    import asyncio

    from app.api.routes.listings import search_listings as shared_search

    marker = "mcpparity-" + uuid.uuid4().hex[:8]
    in_name = _create(marker, name="Zephyr Widget", description="Nothing special here.")
    in_description = _create(marker, name="Other Thing", description="This mentions zephyr deep in the text.")

    rest_page = _search("zephyr", marker)
    mcp_page = asyncio.run(shared_search(q="zephyr", listing_type=marker))

    assert _ids(rest_page) == [in_name["id"], in_description["id"]]
    assert [item.id for item in mcp_page.listings] == [in_name["id"], in_description["id"]]
