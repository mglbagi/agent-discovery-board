"""Duplicate detection: an ACTIVE listing with the same normalized endpoint_url and
submitted_by blocks a second POST with 409 duplicate_listing - and an unsigned POST
never modifies the listing it collided with."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app.core.endpoint import normalize_endpoint_url
from app.core.models import ListingCreate
from app.main import app
from tests.helpers import assert_error, db_row, listing_payload, wallet_auth_header

client = TestClient(app)


def _owner_and_listing(**overrides):
    owner = Account.create()
    payload = listing_payload("offering", owner.address, **overrides)
    response = client.post("/listings", json=payload)
    assert response.status_code == 201, response.text
    return owner, payload, response.json()


def test_duplicate_is_409_with_existing_id_and_next_actions() -> None:
    owner, payload, created = _owner_and_listing()
    response = client.post("/listings", json=payload)
    body = assert_error(response, 409, "duplicate_listing")
    assert body["existing_listing_id"] == created["id"]

    by_key = {(a["method"], a["path"]): a for a in body["next_actions"]}
    heartbeat = by_key[("POST", f"/listings/{created['id']}/heartbeat")]
    patch = by_key[("PATCH", f"/listings/{created['id']}")]
    assert "header:X-Wallet-Auth" in heartbeat["required_fields"]
    assert "header:X-Wallet-Auth" in patch["required_fields"]


def test_unsigned_post_never_modifies_the_existing_listing() -> None:
    owner, payload, created = _owner_and_listing()
    before = db_row(created["id"])

    hostile = {
        **payload,
        "name": "TAKEN OVER",
        "description": "an unsigned POST must not be able to rewrite this",
        "pricing_amount": "$999",
        "task_categories": ["translation"],
        "payment_wallet": Account.create().address,
        "payment_options": [
            {
                "network": "eip155:8453",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "pay_to": Account.create().address,
            }
        ],
        "verification_agent_id": "attacker-chosen-id",
    }
    assert_error(client.post("/listings", json=hostile), 409, "duplicate_listing")

    assert db_row(created["id"]) == before  # every column, updated_at included, is untouched
    assert client.get(f"/listings/{created['id']}").json()["name"] == payload["name"]


def test_same_endpoint_from_a_different_submitted_by_is_allowed() -> None:
    owner, payload, created = _owner_and_listing()
    other = Account.create()
    second = client.post("/listings", json={**payload, "submitted_by": other.address, "payment_wallet": other.address})
    assert second.status_code == 201
    assert second.json()["id"] != created["id"]
    assert second.json()["endpoint_url"] == created["endpoint_url"]


@pytest.mark.parametrize(
    "variant",
    [
        "HTTPS://{host}/{path}",
        "https://{host}/{path}/",
        "https://{host}:443/{path}",
        "https://{host}/{path}#section",
        "https://{host}//{path}",
        "https://user:pw@{host}/{path}",
    ],
)
def test_normalized_variants_of_the_same_url_are_duplicates(variant: str) -> None:
    host, path = f"Host-{uuid.uuid4().hex[:8]}.example.com", "agents/x"
    owner = Account.create()
    first = listing_payload("offering", owner.address, endpoint_url=f"https://{host}/{path}")
    assert client.post("/listings", json=first).status_code == 201

    dup = listing_payload("offering", owner.address, endpoint_url=variant.format(host=host.upper(), path=path))
    assert_error(client.post("/listings", json=dup), 409, "duplicate_listing")


def test_query_parameter_order_does_not_matter() -> None:
    owner = Account.create()
    base = f"https://q-{uuid.uuid4().hex[:8]}.example.com/a"
    assert client.post("/listings", json=listing_payload("offering", owner.address, endpoint_url=f"{base}?a=1&b=2")).status_code == 201
    dup = client.post("/listings", json=listing_payload("offering", owner.address, endpoint_url=f"{base}?b=2&a=1"))
    assert_error(dup, 409, "duplicate_listing")


def test_submitted_by_is_compared_case_insensitively() -> None:
    owner, payload, created = _owner_and_listing()
    shouty = {**payload, "submitted_by": "0x" + owner.address[2:].upper()}
    assert_error(client.post("/listings", json=shouty), 409, "duplicate_listing")


def test_a_different_path_is_not_a_duplicate() -> None:
    owner, payload, created = _owner_and_listing()
    other = {**payload, "endpoint_url": payload["endpoint_url"] + "/v2"}
    assert client.post("/listings", json=other).status_code == 201


def test_an_inactive_listing_does_not_block_a_new_one() -> None:
    owner, payload, created = _owner_and_listing()
    header = wallet_auth_header(owner, action="delete-listing", listing_id=created["id"])
    assert client.delete(f"/listings/{created['id']}", headers={"X-Wallet-Auth": header}).status_code == 200

    again = client.post("/listings", json=payload)
    assert again.status_code == 201
    assert again.json()["id"] != created["id"]


def test_patching_an_endpoint_into_a_duplicate_is_rejected() -> None:
    owner = Account.create()
    a = client.post("/listings", json=listing_payload("offering", owner.address)).json()
    b = client.post("/listings", json=listing_payload("offering", owner.address)).json()

    patch = {"endpoint_url": a["endpoint_url"].replace("example.com", "EXAMPLE.COM") + "/"}  # same after normalizing
    header = wallet_auth_header(owner, action="update-listing", listing_id=b["id"], body=patch)
    response = client.patch(f"/listings/{b['id']}", json=patch, headers={"X-Wallet-Auth": header})
    body = assert_error(response, 409, "duplicate_listing")
    assert body["existing_listing_id"] == a["id"]
    assert client.get(f"/listings/{b['id']}").json()["endpoint_url"] == b["endpoint_url"]


def test_reactivating_into_a_duplicate_is_rejected() -> None:
    owner, payload, first = _owner_and_listing()
    header = wallet_auth_header(owner, action="delete-listing", listing_id=first["id"])
    client.delete(f"/listings/{first['id']}", headers={"X-Wallet-Auth": header})
    second = client.post("/listings", json=payload).json()  # allowed: the first is inactive

    patch = {"status": "active"}
    header = wallet_auth_header(owner, action="update-listing", listing_id=first["id"], body=patch)
    response = client.patch(f"/listings/{first['id']}", json=patch, headers={"X-Wallet-Auth": header})
    body = assert_error(response, 409, "duplicate_listing")
    assert body["existing_listing_id"] == second["id"]


def test_patching_a_listing_never_collides_with_itself() -> None:
    owner, payload, created = _owner_and_listing()
    patch = {"description": "still me", "status": "active"}
    header = wallet_auth_header(owner, action="update-listing", listing_id=created["id"], body=patch)
    assert client.patch(f"/listings/{created['id']}", json=patch, headers={"X-Wallet-Auth": header}).status_code == 200


def test_concurrent_identical_posts_create_exactly_one() -> None:
    owner = Account.create()
    payload = listing_payload("offering", owner.address)

    def post(_):
        return TestClient(app).post("/listings", json=payload).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = sorted(pool.map(post, range(8)))
    assert statuses == [201] + [409] * 7


def test_normalization_function() -> None:
    assert normalize_endpoint_url("HTTPS://Example.COM:443/a//b/?z=1&a=2#f") == "https://example.com/a/b?a=2&z=1"
    assert normalize_endpoint_url("https://example.com/") == normalize_endpoint_url("https://example.com")
    assert normalize_endpoint_url("https://example.com:8443/x") != normalize_endpoint_url("https://example.com/x")
    assert normalize_endpoint_url("https://example.com/X") != normalize_endpoint_url("https://example.com/x")


# ---- only offerings are guarded ------------------------------------------------------------------


def _post(listing_type: str, owner, endpoint_url: str, **overrides):
    payload = listing_payload(listing_type, owner.address, endpoint_url=endpoint_url, **overrides)
    return client.post("/listings", json=payload)


@pytest.mark.parametrize("listing_type", ["announcement", "notice", "request", "collaboration-offer"])
def test_the_same_endpoint_and_submitter_may_repeat_for_non_offering_types(listing_type: str) -> None:
    owner = Account.create()
    url = f"https://repeat-{uuid.uuid4().hex[:8]}.example.com/agents/invoice-extractor"
    ids = []
    for i in range(3):
        response = _post(listing_type, owner, url, name=f"{listing_type} update {i}")
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    assert len(set(ids)) == 3


def test_announcements_and_requests_can_share_an_offerings_endpoint() -> None:
    owner = Account.create()
    url = f"https://shared-{uuid.uuid4().hex[:8]}.example.com/agents/x"
    assert _post("offering", owner, url).status_code == 201
    for listing_type in ("announcement", "announcement", "notice", "request", "request"):
        assert _post(listing_type, owner, url).status_code == 201


def test_a_second_offering_is_still_a_duplicate_even_around_announcements() -> None:
    owner = Account.create()
    url = f"https://after-{uuid.uuid4().hex[:8]}.example.com/agents/x"
    assert _post("announcement", owner, url).status_code == 201
    first = _post("offering", owner, url)
    assert first.status_code == 201
    assert _post("announcement", owner, url).status_code == 201
    body = assert_error(_post("offering", owner, url), 409, "duplicate_listing")
    assert body["existing_listing_id"] == first.json()["id"]  # never points at an announcement


def test_the_database_guard_is_a_partial_index_on_offerings_only() -> None:
    from app.core import db

    def row(listing_type: str) -> dict:
        now = datetime.now(timezone.utc)
        model = ListingCreate(**listing_payload(listing_type, "0x" + "ab" * 20, endpoint_url="https://index-check.example.com/a"))
        return {**model.model_dump(), "id": str(uuid.uuid4()), "status": "active", "is_test": False, "created_at": now, "updated_at": now}

    for _ in range(3):
        db.create_listing(row("announcement"))  # no violation
    db.create_listing(row("offering"))
    with pytest.raises(db.UniqueViolation):
        db.create_listing(row("offering"))  # the index itself, not just the pre-check, refuses this


def test_concurrent_identical_announcements_all_succeed() -> None:
    owner = Account.create()
    payload = listing_payload("announcement", owner.address, endpoint_url=f"https://conc-{uuid.uuid4().hex[:8]}.example.com/x")

    def post(_):
        return TestClient(app).post("/listings", json=payload).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(post, range(8))) == [201] * 8


def test_patching_a_request_into_an_offering_that_duplicates_one_is_rejected() -> None:
    owner = Account.create()
    url = f"https://convert-{uuid.uuid4().hex[:8]}.example.com/x"
    offering = _post("offering", owner, url).json()
    request = _post("request", owner, url).json()

    patch = {"listing_type": "offering"}
    header = wallet_auth_header(owner, action="update-listing", listing_id=request["id"], body=patch)
    response = client.patch(f"/listings/{request['id']}", json=patch, headers={"X-Wallet-Auth": header})
    body = assert_error(response, 409, "duplicate_listing")
    assert body["existing_listing_id"] == offering["id"]
    assert client.get(f"/listings/{request['id']}").json()["listing_type"] == "request"


def test_patching_a_request_into_an_offering_with_a_free_endpoint_is_allowed() -> None:
    owner = Account.create()
    request = _post("request", owner, f"https://free-{uuid.uuid4().hex[:8]}.example.com/x").json()
    patch = {"listing_type": "offering"}
    header = wallet_auth_header(owner, action="update-listing", listing_id=request["id"], body=patch)
    assert client.patch(f"/listings/{request['id']}", json=patch, headers={"X-Wallet-Auth": header}).status_code == 200


def test_non_offerings_can_be_edited_to_the_same_endpoint_and_reactivated_freely() -> None:
    owner = Account.create()
    url = f"https://same-{uuid.uuid4().hex[:8]}.example.com/x"
    a = _post("announcement", owner, url).json()
    b = _post("announcement", owner, url + "/other").json()

    patch = {"endpoint_url": url}
    header = wallet_auth_header(owner, action="update-listing", listing_id=b["id"], body=patch)
    assert client.patch(f"/listings/{b['id']}", json=patch, headers={"X-Wallet-Auth": header}).status_code == 200

    header = wallet_auth_header(owner, action="delete-listing", listing_id=a["id"])
    client.delete(f"/listings/{a['id']}", headers={"X-Wallet-Auth": header})
    patch = {"status": "active"}
    header = wallet_auth_header(owner, action="update-listing", listing_id=a["id"], body=patch)
    assert client.patch(f"/listings/{a['id']}", json=patch, headers={"X-Wallet-Auth": header}).status_code == 200


def test_the_manifest_says_only_offerings_are_guarded() -> None:
    params = client.get("/.well-known/agent-card.json").json()["capabilities"]["extensions"][0]["params"]
    rule = params["duplicateDetection"]
    assert rule["appliesToListingTypes"] == ["offering"]
    assert "announcements, notices and requests may repeat" in rule["rule"]
    assert "OFFERING" in rule["rule"]
