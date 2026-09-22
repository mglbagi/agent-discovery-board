import json

from eth_account import Account
from fastapi.testclient import TestClient

from app.core.limits import MAX_BODY_BYTES
from app.main import app
from tests.helpers import listing_payload

client = TestClient(app)
OWNER = Account.create()


def test_oversized_body_is_rejected_with_413() -> None:
    payload = listing_payload("offering", OWNER.address, description="x" * (MAX_BODY_BYTES + 1000))
    response = client.post(
        "/listings",
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_sql_injection_shaped_search_query_does_not_error_or_leak_data() -> None:
    malicious = "'; DROP TABLE listings; --"
    response = client.get("/listings", params={"q": malicious})
    assert response.status_code == 200  # parameterized queries: this is just a search string, not SQL

    # The table must still exist and work normally afterwards.
    payload = listing_payload("offering", OWNER.address, name="Still here")
    assert client.post("/listings", json=payload).status_code == 201


def test_sql_injection_shaped_listing_type_filter_is_inert() -> None:
    response = client.get("/listings", params={"listing_type": "offering' OR '1'='1"})
    assert response.status_code == 200
    assert response.json()["listings"] == []


def test_control_characters_rejected_end_to_end() -> None:
    payload = listing_payload("offering", OWNER.address, name="Bad\x00Name")
    response = client.post("/listings", json=payload)
    assert response.status_code == 422


def test_http_endpoint_url_rejected_end_to_end() -> None:
    payload = listing_payload("offering", OWNER.address, endpoint_url="http://insecure.example.com")
    response = client.post("/listings", json=payload)
    assert response.status_code == 422


def test_malformed_wallet_auth_header_on_real_endpoint_is_401() -> None:
    created = client.post("/listings", json=listing_payload("offering", OWNER.address)).json()
    response = client.patch(
        f"/listings/{created['id']}",
        json={"description": "x"},
        headers={"X-Wallet-Auth": "not-valid-base64!!"},
    )
    assert response.status_code == 401


def test_extra_unknown_fields_in_create_are_rejected() -> None:
    payload = listing_payload("offering", OWNER.address)
    payload["is_admin"] = True
    response = client.post("/listings", json=payload)
    assert response.status_code == 422


def test_path_traversal_shaped_listing_id_is_422_not_500() -> None:
    response = client.get("/listings/../../etc/passwd")
    assert response.status_code in (404, 422)


def test_response_never_includes_a_raw_signature_or_private_data() -> None:
    created = client.post("/listings", json=listing_payload("offering", OWNER.address)).json()
    assert "signature" not in created
    assert "private_key" not in created
