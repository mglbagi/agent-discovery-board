import uuid

from eth_account import Account
from fastapi.testclient import TestClient

from app.core.rate_limit import create_limiter
from app.main import app
from tests.helpers import listing_payload, wallet_auth_header

client = TestClient(app)

OWNER = Account.create()
ATTACKER = Account.create()


def _create(listing_type: str = "offering", **overrides) -> dict:
    payload = listing_payload(listing_type, OWNER.address, **overrides)
    response = client.post("/listings", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_offering() -> None:
    body = _create("offering")
    assert body["listing_type"] == "offering"
    assert body["status"] == "active"
    assert body["submitted_by"].lower() == OWNER.address.lower()
    assert body["badge"] is None  # trust-score badges are disabled by default in tests
    uuid.UUID(body["id"])  # a real UUID
    assert body["created_at"] == body["updated_at"]


def test_create_request() -> None:
    body = _create("request", task_categories=["code review"])
    assert body["listing_type"] == "request"
    assert body["task_categories"] == ["code review"]


def test_create_announcement_has_no_pricing() -> None:
    body = _create("announcement")
    assert body["pricing_model"] is None
    assert body["pricing_amount"] is None


def test_create_notice_has_no_pricing() -> None:
    body = _create("notice")
    assert body["listing_type"] == "notice"
    assert body["pricing_model"] is None


def test_create_announcement_with_pricing_is_rejected() -> None:
    payload = listing_payload("announcement", OWNER.address, pricing_model="per_call", pricing_amount="$0.05")
    response = client.post("/listings", json=payload)
    assert response.status_code == 422


def test_create_with_invalid_wallet_is_rejected() -> None:
    payload = listing_payload("offering", "not-a-wallet")
    response = client.post("/listings", json=payload)
    assert response.status_code == 422


def test_create_with_unknown_task_category_is_rejected() -> None:
    payload = listing_payload("offering", OWNER.address, task_categories=["not-a-real-category"])
    response = client.post("/listings", json=payload)
    assert response.status_code == 422


def test_get_listing_by_id() -> None:
    created = _create("offering")
    response = client.get(f"/listings/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_nonexistent_listing_is_404() -> None:
    response = client.get(f"/listings/{uuid.uuid4()}")
    assert response.status_code == 404


def test_get_malformed_id_is_422_not_500() -> None:
    response = client.get("/listings/not-a-uuid")
    assert response.status_code == 422


def test_list_includes_created_listings() -> None:
    created = _create("offering")
    response = client.get("/listings")
    assert response.status_code == 200
    body = response.json()
    ids = [item["id"] for item in body["listings"]]
    assert created["id"] in ids
    assert body["total"] >= 1


# ---- PATCH ------------------------------------------------------------------------


def test_patch_by_owner_succeeds() -> None:
    created = _create("offering")
    patch = {"description": "An updated description."}
    header = wallet_auth_header(OWNER, action="update-listing", listing_id=created["id"], body=patch)

    response = client.patch(f"/listings/{created['id']}", json=patch, headers={"X-Wallet-Auth": header})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["description"] == "An updated description."
    assert body["updated_at"] != created["updated_at"]


def test_patch_without_header_is_401() -> None:
    created = _create("offering")
    response = client.patch(f"/listings/{created['id']}", json={"description": "x"})
    assert response.status_code == 401


def test_patch_by_non_owner_is_403() -> None:
    created = _create("offering")
    patch = {"description": "hijacked"}
    header = wallet_auth_header(ATTACKER, action="update-listing", listing_id=created["id"], body=patch)

    response = client.patch(f"/listings/{created['id']}", json=patch, headers={"X-Wallet-Auth": header})

    assert response.status_code == 403
    assert client.get(f"/listings/{created['id']}").json()["description"] != "hijacked"


def test_patch_with_body_that_does_not_match_the_signed_body_is_403() -> None:
    created = _create("offering")
    signed_patch = {"description": "what I signed"}
    header = wallet_auth_header(OWNER, action="update-listing", listing_id=created["id"], body=signed_patch)

    sent_patch = {"description": "what I actually send instead"}
    response = client.patch(f"/listings/{created['id']}", json=sent_patch, headers={"X-Wallet-Auth": header})

    assert response.status_code == 403


def test_patch_nonexistent_listing_is_404_even_without_auth_header() -> None:
    response = client.patch(f"/listings/{uuid.uuid4()}", json={"description": "x"})
    assert response.status_code == 404


def test_patch_empty_body_is_422() -> None:
    created = _create("offering")
    header = wallet_auth_header(OWNER, action="update-listing", listing_id=created["id"], body={})
    response = client.patch(f"/listings/{created['id']}", json={}, headers={"X-Wallet-Auth": header})
    assert response.status_code == 422


def test_patch_cannot_introduce_inconsistent_pricing() -> None:
    created = _create("announcement")
    patch = {"pricing_model": "per_call", "pricing_amount": "$0.10"}
    header = wallet_auth_header(OWNER, action="update-listing", listing_id=created["id"], body=patch)
    response = client.patch(f"/listings/{created['id']}", json=patch, headers={"X-Wallet-Auth": header})
    assert response.status_code == 422


def test_patch_pricing_checked_against_merged_state_not_just_the_patch() -> None:
    # Changing only listing_type to a non-priceable type, while pricing fields from
    # the ORIGINAL listing are still set, must be rejected even though the patch
    # itself never mentions pricing_model/pricing_amount.
    created = _create("offering")  # has pricing_model/pricing_amount set
    patch = {"listing_type": "notice"}
    header = wallet_auth_header(OWNER, action="update-listing", listing_id=created["id"], body=patch)
    response = client.patch(f"/listings/{created['id']}", json=patch, headers={"X-Wallet-Auth": header})
    assert response.status_code == 422


def test_patch_cannot_set_submitted_by() -> None:
    created = _create("offering")
    patch = {"submitted_by": ATTACKER.address}
    header = wallet_auth_header(OWNER, action="update-listing", listing_id=created["id"], body=patch)
    response = client.patch(f"/listings/{created['id']}", json=patch, headers={"X-Wallet-Auth": header})
    assert response.status_code == 422  # ListingUpdate has no submitted_by field at all


# ---- DELETE (soft) ------------------------------------------------------------------


def test_delete_by_owner_deactivates() -> None:
    created = _create("offering")
    header = wallet_auth_header(OWNER, action="delete-listing", listing_id=created["id"], body=None)

    response = client.delete(f"/listings/{created['id']}", headers={"X-Wallet-Auth": header})

    assert response.status_code == 200
    assert response.json()["status"] == "inactive"
    # A soft delete: the row still exists and is fetchable directly by id.
    assert client.get(f"/listings/{created['id']}").json()["status"] == "inactive"


def test_deleted_listing_is_excluded_from_default_browse() -> None:
    created = _create("offering")
    header = wallet_auth_header(OWNER, action="delete-listing", listing_id=created["id"], body=None)
    client.delete(f"/listings/{created['id']}", headers={"X-Wallet-Auth": header})

    default_browse = client.get("/listings").json()
    assert created["id"] not in [item["id"] for item in default_browse["listings"]]

    inactive_browse = client.get("/listings", params={"status": "inactive"}).json()
    assert created["id"] in [item["id"] for item in inactive_browse["listings"]]


def test_delete_without_header_is_401() -> None:
    created = _create("offering")
    response = client.delete(f"/listings/{created['id']}")
    assert response.status_code == 401


def test_delete_by_non_owner_is_403() -> None:
    created = _create("offering")
    header = wallet_auth_header(ATTACKER, action="delete-listing", listing_id=created["id"], body=None)
    response = client.delete(f"/listings/{created['id']}", headers={"X-Wallet-Auth": header})
    assert response.status_code == 403
    assert client.get(f"/listings/{created['id']}").json()["status"] == "active"


def test_delete_nonexistent_listing_is_404() -> None:
    response = client.delete(f"/listings/{uuid.uuid4()}")
    assert response.status_code == 404


def test_update_signature_cannot_be_replayed_as_a_delete() -> None:
    created = _create("offering")
    patch = {"description": "fine"}
    update_header = wallet_auth_header(OWNER, action="update-listing", listing_id=created["id"], body=patch)

    response = client.delete(f"/listings/{created['id']}", headers={"X-Wallet-Auth": update_header})

    assert response.status_code == 403


# ---- rate limiting on creation ------------------------------------------------------


def test_creation_is_rate_limited(monkeypatch) -> None:
    monkeypatch.setattr(create_limiter, "max_requests", 2)
    monkeypatch.setattr(create_limiter, "window_seconds", 60.0)
    create_limiter.reset()
    try:
        for _ in range(2):
            payload = listing_payload("offering", OWNER.address)
            assert client.post("/listings", json=payload).status_code == 201

        response = client.post("/listings", json=listing_payload("offering", OWNER.address))
        assert response.status_code == 429
    finally:
        create_limiter.reset()
