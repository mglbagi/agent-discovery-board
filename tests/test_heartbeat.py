"""POST /listings/{id}/heartbeat: signed with the same wallet scheme as PATCH (replay
protected), at most once per 24h, sets last_seen_at."""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app.core import wallet_auth
from app.core.activity import HEARTBEAT_MIN_INTERVAL
from app.main import app
from tests.helpers import assert_error, db_row, listing_payload, set_listing_times, wallet_auth_header

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_nonces():
    wallet_auth._seen_nonces.clear()
    yield
    wallet_auth._seen_nonces.clear()


def _listing():
    owner = Account.create()
    response = client.post("/listings", json=listing_payload("offering", owner.address))
    assert response.status_code == 201
    return owner, response.json()


def _beat(owner, listing_id: str, **kw):
    header = wallet_auth_header(owner, action="heartbeat-listing", listing_id=listing_id, **kw)
    return client.post(f"/listings/{listing_id}/heartbeat", headers={"X-Wallet-Auth": header})


def test_heartbeat_sets_last_seen_at_and_reports_the_next_allowed_time() -> None:
    owner, listing = _listing()
    assert listing["last_seen_at"] is None

    response = _beat(owner, listing["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"id", "last_seen_at", "next_heartbeat_allowed_at", "last_activity_at", "stale"}
    assert body["id"] == listing["id"] and body["stale"] is False

    from datetime import datetime

    seen = datetime.fromisoformat(body["last_seen_at"])
    assert datetime.fromisoformat(body["next_heartbeat_allowed_at"]) == seen + HEARTBEAT_MIN_INTERVAL
    assert client.get(f"/listings/{listing['id']}").json()["last_seen_at"] == body["last_seen_at"]


def test_heartbeat_does_not_touch_updated_at_or_any_other_field() -> None:
    owner, listing = _listing()
    before = db_row(listing["id"])
    assert _beat(owner, listing["id"]).status_code == 200
    after = db_row(listing["id"])
    assert {k: v for k, v in after.items() if k != "last_seen_at"} == {k: v for k, v in before.items() if k != "last_seen_at"}


def test_a_second_heartbeat_within_24h_is_rate_limited_with_retry_after() -> None:
    owner, listing = _listing()
    assert _beat(owner, listing["id"]).status_code == 200

    response = _beat(owner, listing["id"])
    body = assert_error(response, 429, "rate_limited")
    assert HEARTBEAT_MIN_INTERVAL.total_seconds() - 60 <= body["retry_after"] <= HEARTBEAT_MIN_INTERVAL.total_seconds()
    assert response.headers["retry-after"] == str(body["retry_after"])
    retry = body["next_actions"][0]
    assert (retry["method"], retry["path"]) == ("POST", f"/listings/{listing['id']}/heartbeat")
    assert retry["required_fields"] == ["header:X-Wallet-Auth"]  # a repeat needs a fresh signature
    assert "freshly signed" in retry["description"]


def test_retry_after_shrinks_as_the_window_runs_down() -> None:
    owner, listing = _listing()
    assert _beat(owner, listing["id"]).status_code == 200
    set_listing_times(listing["id"], last_seen_hours_ago=23.5)  # 30 minutes left
    body = assert_error(_beat(owner, listing["id"]), 429, "rate_limited")
    assert 1700 <= body["retry_after"] <= 1800


def test_a_heartbeat_is_accepted_again_after_the_interval() -> None:
    owner, listing = _listing()
    assert _beat(owner, listing["id"]).status_code == 200
    set_listing_times(listing["id"], last_seen_hours_ago=24.5)
    assert _beat(owner, listing["id"]).status_code == 200


def test_the_wrong_signer_is_rejected_and_nothing_is_recorded() -> None:
    owner, listing = _listing()
    attacker = Account.create()
    assert_error(_beat(attacker, listing["id"]), 403, "wrong_signer")
    assert db_row(listing["id"])["last_seen_at"] is None


def test_missing_and_malformed_signatures_are_rejected() -> None:
    owner, listing = _listing()
    assert_error(client.post(f"/listings/{listing['id']}/heartbeat"), 401, "missing_signature")
    bad = client.post(f"/listings/{listing['id']}/heartbeat", headers={"X-Wallet-Auth": "!!!"})
    assert_error(bad, 401, "malformed_signature")
    assert db_row(listing["id"])["last_seen_at"] is None


def test_a_stale_signature_is_rejected() -> None:
    owner, listing = _listing()
    response = _beat(owner, listing["id"], timestamp=int(time.time()) - 100_000)
    assert_error(response, 401, "stale_signature")


def test_a_captured_heartbeat_cannot_be_replayed() -> None:
    owner, listing = _listing()
    header = wallet_auth_header(owner, action="heartbeat-listing", listing_id=listing["id"])
    url = f"/listings/{listing['id']}/heartbeat"
    assert client.post(url, headers={"X-Wallet-Auth": header}).status_code == 200
    set_listing_times(listing["id"], last_seen_hours_ago=30)  # window open again, so only the nonce stops it
    assert_error(client.post(url, headers={"X-Wallet-Auth": header}), 401, "replayed_signature")


def test_signatures_for_other_actions_cannot_be_used_as_a_heartbeat() -> None:
    owner, listing = _listing()
    for action, body in (("delete-listing", None), ("update-listing", {})):
        header = wallet_auth_header(owner, action=action, listing_id=listing["id"], body=body)
        response = client.post(f"/listings/{listing['id']}/heartbeat", headers={"X-Wallet-Auth": header})
        assert_error(response, 403, "wrong_signer")
    assert db_row(listing["id"])["last_seen_at"] is None
    assert db_row(listing["id"])["status"] == "active"


def test_a_heartbeat_signature_for_one_listing_does_not_work_for_another() -> None:
    owner = Account.create()
    a = client.post("/listings", json=listing_payload("offering", owner.address)).json()
    b = client.post("/listings", json=listing_payload("offering", owner.address)).json()
    header = wallet_auth_header(owner, action="heartbeat-listing", listing_id=a["id"])
    response = client.post(f"/listings/{b['id']}/heartbeat", headers={"X-Wallet-Auth": header})
    assert_error(response, 403, "wrong_signer")


def test_unknown_listing_is_404() -> None:
    owner = Account.create()
    assert_error(client.post(f"/listings/{uuid.uuid4()}/heartbeat"), 404, "not_found")


def test_an_inactive_listing_cannot_heartbeat() -> None:
    owner, listing = _listing()
    header = wallet_auth_header(owner, action="delete-listing", listing_id=listing["id"])
    client.delete(f"/listings/{listing['id']}", headers={"X-Wallet-Auth": header})

    body = assert_error(_beat(owner, listing["id"]), 409, "listing_inactive")
    assert body["next_actions"][0]["method"] == "PATCH"
    assert "status" in body["next_actions"][0]["required_fields"]


def test_concurrent_heartbeats_record_exactly_one() -> None:
    owner, listing = _listing()

    def beat(_):
        header = wallet_auth_header(owner, action="heartbeat-listing", listing_id=listing["id"])
        return TestClient(app).post(f"/listings/{listing['id']}/heartbeat", headers={"X-Wallet-Auth": header}).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = sorted(pool.map(beat, range(6)))
    assert statuses == [200] + [429] * 5


def test_heartbeat_response_documented_in_openapi() -> None:
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/listings/{listing_id}/heartbeat"]["post"]
    assert {"200", "401", "403", "404", "409", "429"} <= set(op["responses"])
    assert op["responses"]["429"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
