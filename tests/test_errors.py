"""Machine-readable errors: every error response, from any route, dependency or
middleware, has the same shape - stable error_code, message, detail, next_actions."""

import json
import re
import time
import uuid
from pathlib import Path

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app.core import errors, wallet_auth
from app.core.errors import ERROR_CODES, ApiError, error_codes_manifest
from app.core.limits import MAX_BODY_BYTES
from app.core.rate_limit import create_limiter
from app.main import app
from tests.helpers import assert_error, listing_payload, wallet_auth_header

client = TestClient(app)
OWNER = Account.create()
OTHER = Account.create()


@pytest.fixture(autouse=True)
def _clean_nonces():
    wallet_auth._seen_nonces.clear()
    yield
    wallet_auth._seen_nonces.clear()


def _create() -> dict:
    response = client.post("/listings", json=listing_payload("offering", OWNER.address))
    assert response.status_code == 201, response.text
    return response.json()


# ---- shape across error sources ------------------------------------------------------


def test_not_found_has_search_and_create_next_actions() -> None:
    body = assert_error(client.get(f"/listings/{uuid.uuid4()}"), 404, "not_found")
    actions = {(a["method"], a["path"]): a for a in body["next_actions"]}
    assert ("GET", "/listings") in actions
    create = actions[("POST", "/listings")]
    assert {"name", "description", "listing_type", "task_categories", "endpoint_url", "submitted_by"} <= set(
        create["required_fields"]
    )


def test_unknown_route_is_a_coded_404() -> None:
    assert_error(client.get("/no/such/route"), 404, "not_found")


def test_wrong_method_is_a_coded_405() -> None:
    assert_error(client.put("/listings"), 405, "method_not_allowed")


def test_request_validation_error_keeps_fastapi_detail_list_and_adds_code() -> None:
    body = assert_error(client.post("/listings", json={"name": "only a name"}), 422, "validation_error")
    assert isinstance(body["detail"], list) and body["detail"][0]["loc"]  # unchanged FastAPI-style list
    assert body["message"].startswith("body.")
    post = next(a for a in body["next_actions"] if a["method"] == "POST" and a["path"] == "/listings")
    assert "endpoint_url" in post["required_fields"]


def test_query_validation_error_is_coded() -> None:
    assert_error(client.get("/listings", params={"limit": 0}), 422, "validation_error")


def test_unknown_task_category_filter_has_its_own_code() -> None:
    assert_error(client.get("/listings", params={"task_category": "nope"}), 422, "invalid_task_category")


def test_empty_patch_code() -> None:
    created = _create()
    header = wallet_auth_header(OWNER, action="update-listing", listing_id=created["id"], body={})
    response = client.patch(f"/listings/{created['id']}", json={}, headers={"X-Wallet-Auth": header})
    assert_error(response, 422, "empty_patch")


def test_oversized_body_is_a_coded_413() -> None:
    payload = listing_payload("offering", OWNER.address, description="x" * (MAX_BODY_BYTES + 10))
    response = client.post("/listings", content=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    assert_error(response, 413, "body_too_large")


def test_rate_limited_has_retry_after_in_body_and_header(monkeypatch) -> None:
    monkeypatch.setattr(create_limiter, "max_requests", 1)
    create_limiter.reset()
    try:
        assert client.post("/listings", json=listing_payload("offering", OWNER.address)).status_code == 201
        response = client.post("/listings", json=listing_payload("offering", OWNER.address))
        body = assert_error(response, 429, "rate_limited")
        assert isinstance(body["retry_after"], int) and body["retry_after"] >= 1
        assert response.headers["retry-after"] == str(body["retry_after"])
        retry = body["next_actions"][0]
        assert (retry["method"], retry["path"]) == ("POST", "/listings")
        assert retry["required_fields"] == []  # POST /listings is unsigned
        assert str(body["retry_after"]) in retry["description"]
    finally:
        create_limiter.reset()


def test_unhandled_exception_is_a_coded_500(monkeypatch) -> None:
    from app.core import db

    def boom(*a, **k):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(db, "get_listing", boom)
    response = TestClient(app, raise_server_exceptions=False).get(f"/listings/{uuid.uuid4()}")
    body = assert_error(response, 500, "internal_error")
    assert "exploded" not in json.dumps(body)  # never leak internals


# ---- signature error codes ------------------------------------------------------------


def test_missing_signature() -> None:
    created = _create()
    body = assert_error(client.patch(f"/listings/{created['id']}", json={"name": "x"}), 401, "missing_signature")
    retry = body["next_actions"][0]
    assert (retry["method"], retry["path"]) == ("PATCH", f"/listings/{created['id']}")
    assert "header:X-Wallet-Auth" in retry["required_fields"]
    assert any(a["path"] == "/.well-known/agent-card.json" for a in body["next_actions"])


def test_malformed_signature() -> None:
    created = _create()
    response = client.patch(f"/listings/{created['id']}", json={"name": "x"}, headers={"X-Wallet-Auth": "%%%"})
    assert_error(response, 401, "malformed_signature")


def test_stale_signature_reports_server_time_and_window() -> None:
    created = _create()
    patch = {"name": "x"}
    header = wallet_auth_header(
        OWNER, action="update-listing", listing_id=created["id"], body=patch, timestamp=int(time.time()) - 100_000
    )
    response = client.patch(f"/listings/{created['id']}", json=patch, headers={"X-Wallet-Auth": header})
    body = assert_error(response, 401, "stale_signature")
    assert abs(body["server_time"] - time.time()) < 60
    assert body["max_age_seconds"] == int(wallet_auth.SIGNATURE_MAX_AGE_SECONDS)


def test_replayed_signature() -> None:
    created = _create()
    patch = {"name": "renamed once"}
    header = wallet_auth_header(OWNER, action="update-listing", listing_id=created["id"], body=patch)
    assert client.patch(f"/listings/{created['id']}", json=patch, headers={"X-Wallet-Auth": header}).status_code == 200
    replay = client.patch(f"/listings/{created['id']}", json=patch, headers={"X-Wallet-Auth": header})
    assert_error(replay, 401, "replayed_signature")


def test_invalid_signature_bytes() -> None:
    import base64

    created = _create()
    payload = {"signature": "0x" + "00" * 65, "timestamp": int(time.time()), "nonce": uuid.uuid4().hex}
    header = base64.b64encode(json.dumps(payload).encode()).decode()
    response = client.patch(f"/listings/{created['id']}", json={"name": "x"}, headers={"X-Wallet-Auth": header})
    assert_error(response, 401, "invalid_signature")


def test_wrong_signer_is_403_with_a_way_forward() -> None:
    created = _create()
    patch = {"name": "hijack"}
    header = wallet_auth_header(OTHER, action="update-listing", listing_id=created["id"], body=patch)
    response = client.patch(f"/listings/{created['id']}", json=patch, headers={"X-Wallet-Auth": header})
    body = assert_error(response, 403, "wrong_signer")
    assert any(a["method"] == "GET" and a["path"] == f"/listings/{created['id']}" for a in body["next_actions"])


# ---- the registry -----------------------------------------------------------------------


def test_apierror_refuses_an_unregistered_code() -> None:
    with pytest.raises(RuntimeError):
        ApiError(400, "made_up_code", "x")


def test_every_code_has_a_status_and_description() -> None:
    for code, spec in ERROR_CODES.items():
        assert re.fullmatch(r"[a-z][a-z_]*", code)
        assert 400 <= spec.http_status <= 599 and spec.description


def test_every_code_the_source_can_emit_is_registered() -> None:
    root = Path(errors.__file__).resolve().parent.parent
    used = set()
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        used |= set(re.findall(r'ApiError\(\s*\d{3},\s*"([a-z_]+)"', text))
        used |= set(re.findall(r'code="([a-z_]+)"', text))
    assert used, "scan found nothing - the pattern is stale"
    assert used <= set(ERROR_CODES), used - set(ERROR_CODES)
    assert set(errors._STATUS_DEFAULT_CODE.values()) <= set(ERROR_CODES)


def test_manifest_publishes_exactly_the_registry() -> None:
    card = client.get("/.well-known/agent-card.json").json()
    published = card["capabilities"]["extensions"][0]["params"]["errors"]["codes"]
    assert published == error_codes_manifest()
    assert {c["error_code"] for c in published} == set(ERROR_CODES)


def test_openapi_documents_the_error_shape_and_codes() -> None:
    spec = client.get("/openapi.json").json()
    assert {c["error_code"] for c in spec["info"]["x-error-codes"]} == set(ERROR_CODES)
    assert "ErrorResponse" in spec["components"]["schemas"]
    for code in ERROR_CODES:
        assert f"`{code}`" in spec["info"]["description"]
    post = spec["paths"]["/listings"]["post"]["responses"]
    assert post["409"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
    assert post["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
