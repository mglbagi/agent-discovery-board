import base64
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException

from app.core import wallet_auth
from app.core.wallet_auth import _build_message, verify_wallet_auth

OWNER = Account.create()
ATTACKER = Account.create()


@pytest.fixture(autouse=True)
def _isolated_nonce_cache():
    wallet_auth._seen_nonces.clear()
    yield
    wallet_auth._seen_nonces.clear()


def _fake_request(header_value: str | None) -> SimpleNamespace:
    return SimpleNamespace(headers={"x-wallet-auth": header_value} if header_value is not None else {})


def _sig_hex(signed) -> str:
    raw = signed.signature.hex()
    return raw if raw.startswith("0x") else "0x" + raw


def _header(account, *, action, listing_id, timestamp=None, nonce=None, body=None) -> str:
    timestamp = int(time.time()) if timestamp is None else timestamp
    nonce = nonce or uuid.uuid4().hex
    message = _build_message(action=action, listing_id=listing_id, timestamp=timestamp, nonce=nonce, body=body)
    signed = account.sign_message(encode_defunct(text=message))
    payload = {"signature": _sig_hex(signed), "timestamp": timestamp, "nonce": nonce}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def test_valid_signature_passes() -> None:
    header = _header(OWNER, action="update-listing", listing_id="abc", body={"name": "New"})
    verify_wallet_auth(
        _fake_request(header), action="update-listing", listing_id="abc", submitted_by=OWNER.address, body={"name": "New"}
    )


def test_delete_has_no_body() -> None:
    header = _header(OWNER, action="delete-listing", listing_id="abc", body=None)
    verify_wallet_auth(_fake_request(header), action="delete-listing", listing_id="abc", submitted_by=OWNER.address, body=None)


def test_submitted_by_is_case_insensitive() -> None:
    header = _header(OWNER, action="delete-listing", listing_id="abc", body=None)
    verify_wallet_auth(
        _fake_request(header), action="delete-listing", listing_id="abc", submitted_by=OWNER.address.upper(), body=None
    )


def test_wrong_signer_is_403() -> None:
    header = _header(ATTACKER, action="update-listing", listing_id="abc", body={"name": "New"})
    with pytest.raises(HTTPException) as exc:
        verify_wallet_auth(
            _fake_request(header), action="update-listing", listing_id="abc", submitted_by=OWNER.address, body={"name": "New"}
        )
    assert exc.value.status_code == 403


def test_tampered_body_is_403() -> None:
    # Owner signs one body; the server actually received a DIFFERENT one -> the
    # reconstructed message differs -> recovered signer differs -> 403. There is no
    # separate "signature doesn't match body" check; a mismatch just looks like the
    # wrong signer, which is the point (see app/core/wallet_auth.py docstring).
    header = _header(OWNER, action="update-listing", listing_id="abc", body={"name": "Signed value"})
    with pytest.raises(HTTPException) as exc:
        verify_wallet_auth(
            _fake_request(header),
            action="update-listing",
            listing_id="abc",
            submitted_by=OWNER.address,
            body={"name": "Tampered value"},
        )
    assert exc.value.status_code == 403


def test_tampered_listing_id_is_403() -> None:
    header = _header(OWNER, action="delete-listing", listing_id="abc", body=None)
    with pytest.raises(HTTPException) as exc:
        verify_wallet_auth(
            _fake_request(header), action="delete-listing", listing_id="some-other-id", submitted_by=OWNER.address, body=None
        )
    assert exc.value.status_code == 403


def test_delete_signature_cannot_authorize_update() -> None:
    header = _header(OWNER, action="delete-listing", listing_id="abc", body=None)
    with pytest.raises(HTTPException) as exc:
        verify_wallet_auth(_fake_request(header), action="update-listing", listing_id="abc", submitted_by=OWNER.address, body={})
    assert exc.value.status_code == 403


def test_missing_header_is_401() -> None:
    with pytest.raises(HTTPException) as exc:
        verify_wallet_auth(_fake_request(None), action="delete-listing", listing_id="abc", submitted_by=OWNER.address, body=None)
    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    "raw_header",
    [
        "not-valid-base64!!",
        base64.b64encode(b"not json").decode(),
        base64.b64encode(json.dumps({"signature": "0xabc"}).encode()).decode(),  # missing fields
        base64.b64encode(json.dumps({"signature": 5, "timestamp": 1, "nonce": "n"}).encode()).decode(),  # wrong type
        base64.b64encode(json.dumps({"signature": "0xabc", "timestamp": "not-an-int", "nonce": "n"}).encode()).decode(),
    ],
)
def test_malformed_header_is_401(raw_header: str) -> None:
    with pytest.raises(HTTPException) as exc:
        verify_wallet_auth(_fake_request(raw_header), action="delete-listing", listing_id="abc", submitted_by=OWNER.address, body=None)
    assert exc.value.status_code == 401


def test_garbage_signature_is_401() -> None:
    payload = {"signature": "0x" + "00" * 65, "timestamp": int(time.time()), "nonce": uuid.uuid4().hex}
    header = base64.b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(HTTPException) as exc:
        verify_wallet_auth(_fake_request(header), action="delete-listing", listing_id="abc", submitted_by=OWNER.address, body=None)
    assert exc.value.status_code == 401


def test_stale_timestamp_is_401() -> None:
    old = int(time.time()) - 10_000
    header = _header(OWNER, action="delete-listing", listing_id="abc", timestamp=old, body=None)
    with pytest.raises(HTTPException) as exc:
        verify_wallet_auth(_fake_request(header), action="delete-listing", listing_id="abc", submitted_by=OWNER.address, body=None)
    assert exc.value.status_code == 401


def test_future_timestamp_is_401() -> None:
    future = int(time.time()) + 10_000
    header = _header(OWNER, action="delete-listing", listing_id="abc", timestamp=future, body=None)
    with pytest.raises(HTTPException) as exc:
        verify_wallet_auth(_fake_request(header), action="delete-listing", listing_id="abc", submitted_by=OWNER.address, body=None)
    assert exc.value.status_code == 401


def test_replayed_nonce_is_rejected_even_though_signature_is_valid() -> None:
    nonce = uuid.uuid4().hex
    header = _header(OWNER, action="delete-listing", listing_id="abc", nonce=nonce, body=None)
    verify_wallet_auth(_fake_request(header), action="delete-listing", listing_id="abc", submitted_by=OWNER.address, body=None)

    with pytest.raises(HTTPException) as exc:
        verify_wallet_auth(_fake_request(header), action="delete-listing", listing_id="abc", submitted_by=OWNER.address, body=None)
    assert exc.value.status_code == 401


def test_same_nonce_is_independent_per_wallet() -> None:
    nonce = uuid.uuid4().hex
    owner_header = _header(OWNER, action="delete-listing", listing_id="abc", nonce=nonce, body=None)
    verify_wallet_auth(_fake_request(owner_header), action="delete-listing", listing_id="abc", submitted_by=OWNER.address, body=None)

    attacker_header = _header(ATTACKER, action="delete-listing", listing_id="def", nonce=nonce, body=None)
    verify_wallet_auth(
        _fake_request(attacker_header), action="delete-listing", listing_id="def", submitted_by=ATTACKER.address, body=None
    )  # different wallet -> independent nonce space, does not raise
