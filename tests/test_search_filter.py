import uuid

from eth_account import Account
from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import listing_payload

client = TestClient(app)


def _create(listing_type: str, **overrides) -> dict:
    owner = Account.create()
    payload = listing_payload(listing_type, owner.address, **overrides)
    response = client.post("/listings", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_filter_by_listing_type() -> None:
    offering = _create("offering", name=f"Offering {uuid.uuid4().hex}")
    request = _create("request", name=f"Request {uuid.uuid4().hex}")

    response = client.get("/listings", params={"listing_type": "offering"})
    ids = [item["id"] for item in response.json()["listings"]]
    assert offering["id"] in ids
    assert request["id"] not in ids


def test_filter_by_single_task_category() -> None:
    marker = uuid.uuid4().hex
    extraction = _create("offering", name=f"Extractor {marker}", task_categories=["data extraction"])
    translation = _create("offering", name=f"Translator {marker}", task_categories=["translation"])

    response = client.get("/listings", params={"task_category": "data extraction"})
    ids = [item["id"] for item in response.json()["listings"]]
    assert extraction["id"] in ids
    assert translation["id"] not in ids


def test_filter_by_task_category_matches_any_overlap() -> None:
    marker = uuid.uuid4().hex
    multi = _create("offering", name=f"Multi {marker}", task_categories=["translation", "summarization"])

    response = client.get("/listings", params={"task_category": "summarization"})
    ids = [item["id"] for item in response.json()["listings"]]
    assert multi["id"] in ids


def test_unknown_task_category_filter_is_422() -> None:
    response = client.get("/listings", params={"task_category": "not-a-real-category"})
    assert response.status_code == 422


def test_free_text_search_matches_name() -> None:
    marker = uuid.uuid4().hex
    listing = _create("offering", name=f"UniqueSearchableName-{marker}")

    response = client.get("/listings", params={"q": marker})
    ids = [item["id"] for item in response.json()["listings"]]
    assert listing["id"] in ids


def test_free_text_search_matches_description() -> None:
    marker = uuid.uuid4().hex
    listing = _create("offering", description=f"Handles invoices and receipts, marker {marker}.")

    response = client.get("/listings", params={"q": marker})
    ids = [item["id"] for item in response.json()["listings"]]
    assert listing["id"] in ids


def test_free_text_search_is_case_insensitive() -> None:
    marker = uuid.uuid4().hex.upper()
    listing = _create("offering", name=f"Loud Name {marker}")

    response = client.get("/listings", params={"q": marker.lower()})
    ids = [item["id"] for item in response.json()["listings"]]
    assert listing["id"] in ids


def test_search_with_percent_and_underscore_is_treated_literally() -> None:
    # A caller typing a literal '%' or '_' should search for that literal character,
    # not have it act as a SQL LIKE wildcard (see app/core/db.py's ESCAPE clause).
    marker = uuid.uuid4().hex
    percent_listing = _create("offering", name=f"100%_{marker}_off")
    other_listing = _create("offering", name=f"unrelated_{marker}_xyz")

    response = client.get("/listings", params={"q": f"100%_{marker}"})
    ids = [item["id"] for item in response.json()["listings"]]
    assert percent_listing["id"] in ids
    assert other_listing["id"] not in ids


def test_combined_filters() -> None:
    marker = uuid.uuid4().hex
    match = _create(
        "offering", name=f"Combined {marker}", task_categories=["code review"], description="matches everything"
    )
    wrong_type = _create("request", name=f"Combined {marker}", task_categories=["code review"])
    wrong_category = _create("offering", name=f"Combined {marker}", task_categories=["translation"])

    response = client.get(
        "/listings", params={"listing_type": "offering", "task_category": "code review", "q": marker}
    )
    ids = [item["id"] for item in response.json()["listings"]]
    assert match["id"] in ids
    assert wrong_type["id"] not in ids
    assert wrong_category["id"] not in ids


def test_pagination_limit_and_offset() -> None:
    marker = uuid.uuid4().hex
    created_ids = [_create("offering", name=f"Page {marker} #{i}")["id"] for i in range(5)]

    page1 = client.get("/listings", params={"q": marker, "limit": 2, "offset": 0}).json()
    page2 = client.get("/listings", params={"q": marker, "limit": 2, "offset": 2}).json()

    assert page1["total"] == 5
    assert len(page1["listings"]) == 2
    assert len(page2["listings"]) == 2
    assert {item["id"] for item in page1["listings"]}.isdisjoint({item["id"] for item in page2["listings"]})
    assert {item["id"] for item in page1["listings"]} <= set(created_ids)


def test_limit_is_capped() -> None:
    response = client.get("/listings", params={"limit": 10_000})
    assert response.status_code == 422


def test_status_filter_defaults_to_active_only() -> None:
    marker = uuid.uuid4().hex
    listing = _create("offering", name=f"StatusDefault {marker}")

    all_active = client.get("/listings", params={"q": marker}).json()
    assert listing["id"] in [item["id"] for item in all_active["listings"]]
    assert all(item["status"] == "active" for item in all_active["listings"])
