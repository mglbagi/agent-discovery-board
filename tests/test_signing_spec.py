"""The published signing spec must be sufficient on its own: an agent that has read only
the manifest can sign a request. These tests build signatures using nothing but the
published spec (never the server's own helper) and check the worked example is real."""

import base64
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from app.core import signing_spec as spec_module
from app.core import wallet_auth
from app.core.wallet_auth import SIGNED_ACTIONS, verify_wallet_auth
from app.main import app
from tests.helpers import listing_payload

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_nonces():
    wallet_auth._seen_nonces.clear()
    yield
    wallet_auth._seen_nonces.clear()


def _published_spec() -> dict:
    card = client.get("/.well-known/agent-card.json").json()
    return card["capabilities"]["extensions"][0]["params"]["signingSpec"]


# ---- the worked example is real ---------------------------------------------------------------


def test_the_example_key_matches_the_example_address() -> None:
    assert Account.from_key(spec_module.EXAMPLE_PRIVATE_KEY).address == spec_module.EXAMPLE_ADDRESS


@pytest.mark.parametrize("section", ["update_listing", "heartbeat_listing"])
def test_the_example_signature_recovers_to_the_example_address(section: str) -> None:
    example = _published_spec()["worked_example"][section]
    recovered = Account.recover_message(encode_defunct(text=example["message"]), signature=example["signature"])
    assert recovered == spec_module.EXAMPLE_ADDRESS


def test_the_example_message_matches_the_published_template_exactly() -> None:
    spec = _published_spec()
    example = spec["worked_example"]
    body = example["update_listing"]["request"]["json_body"]
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    expected = "\n".join(
        [
            "Agent Discovery Board",
            "action: update-listing",
            f"listing_id: {example['listing_id']}",
            f"timestamp: {example['timestamp']}",
            f"nonce: {example['nonce']}",
            f"body_sha256: {digest}",
        ]
    )
    assert example["update_listing"]["message"] == expected
    assert example["update_listing"]["body_sha256"] == digest
    assert example["update_listing"]["canonical_body_json"] == json.dumps(body, sort_keys=True, separators=(",", ":"))
    assert not expected.endswith("\n")


def test_the_heartbeat_example_has_no_body_hash_line() -> None:
    message = _published_spec()["worked_example"]["heartbeat_listing"]["message"]
    assert message.splitlines()[1] == "action: heartbeat-listing"
    assert "body_sha256" not in message and len(message.splitlines()) == 5


@pytest.mark.parametrize("section", ["update_listing", "heartbeat_listing"])
def test_the_example_header_is_exactly_what_the_spec_says_it_is(section: str) -> None:
    example = _published_spec()["worked_example"][section]
    decoded = json.loads(base64.b64decode(example["x_wallet_auth_header"], validate=True))
    assert decoded == example["x_wallet_auth_json"]
    assert set(decoded) == {"signature", "timestamp", "nonce"}
    assert decoded["signature"] == example["signature"] and isinstance(decoded["timestamp"], int)


@pytest.mark.parametrize(
    "section, action, has_body",
    [("update_listing", "update-listing", True), ("heartbeat_listing", "heartbeat-listing", False)],
)
def test_the_example_header_is_accepted_by_the_real_verifier(monkeypatch, section: str, action: str, has_body: bool) -> None:
    example = _published_spec()["worked_example"]
    monkeypatch.setattr(wallet_auth.time, "time", lambda: example["timestamp"] + 5)  # inside the window
    request = SimpleNamespace(headers={"x-wallet-auth": example[section]["x_wallet_auth_header"]})
    verify_wallet_auth(
        request,
        action=action,
        listing_id=example["listing_id"],
        submitted_by=example["example_address"],
        body=example["update_listing"]["request"]["json_body"] if has_body else None,
    )  # does not raise


def test_the_example_is_computed_not_typed_in() -> None:
    assert spec_module.worked_example() is spec_module.worked_example()  # cached, deterministic
    assert spec_module.worked_example()["update_listing"]["signature"] == spec_module._sign(
        spec_module.worked_example()["update_listing"]["message"]
    )


# ---- what the spec states ----------------------------------------------------------------------


def test_the_spec_covers_every_signed_action_and_states_the_window() -> None:
    spec = _published_spec()
    assert set(spec["actions"]) == set(SIGNED_ACTIONS)
    assert spec["actions"]["update-listing"]["http"] == "PATCH /listings/{id}"
    assert spec["actions"]["heartbeat-listing"]["http"] == "POST /listings/{id}/heartbeat"
    assert spec["actions"]["heartbeat-listing"]["signs_body_hash"] is False
    assert spec["time_window"]["max_age_seconds"] == int(wallet_auth.SIGNATURE_MAX_AGE_SECONDS)
    assert spec["header"]["name"] == "X-Wallet-Auth"
    assert "EIP-191" in spec["scheme"] and "personal_sign" in spec["scheme"]
    assert set(spec["failure_codes"]) == {
        "missing_signature",
        "malformed_signature",
        "stale_signature",
        "replayed_signature",
        "invalid_signature",
        "wrong_signer",
    }


def test_the_published_spec_is_the_one_the_module_builds() -> None:
    assert _published_spec() == json.loads(json.dumps(spec_module.signing_spec()))


# ---- an agent that reads only the manifest can sign ---------------------------------------------


def _sign_from_the_spec_alone(spec: dict, account, action: str, listing_id: str, body: dict | None) -> str:
    """Deliberately re-implements the message rules from the published spec text only."""
    timestamp, nonce = int(__import__("time").time()), uuid.uuid4().hex
    values = {"action": action, "listing_id": listing_id, "timestamp": str(timestamp), "nonce": nonce}
    lines = [spec["message_template"]["lines"][0]]
    for template_line in spec["message_template"]["lines"][1:]:
        key = template_line.split(":", 1)[0]
        if key == "body_sha256":
            if not spec["actions"][action]["signs_body_hash"]:
                continue
            canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            values["body_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        lines.append(f"{key}: {values[key]}")
    message = "\n".join(lines)
    signature = account.sign_message(encode_defunct(text=message)).signature.hex()
    signature = signature if signature.startswith("0x") else "0x" + signature
    header = {"signature": signature, "timestamp": timestamp, "nonce": nonce}
    return base64.b64encode(json.dumps(header).encode()).decode()


def test_an_agent_holding_only_the_manifest_can_patch_and_heartbeat() -> None:
    spec = _published_spec()
    owner = Account.create()
    listing = client.post("/listings", json=listing_payload("offering", owner.address)).json()

    patch = {"description": "signed using only the published signing spec"}
    header = _sign_from_the_spec_alone(spec, owner, "update-listing", listing["id"], patch)
    header_name = spec["header"]["name"]
    updated = client.patch(f"/listings/{listing['id']}", json=patch, headers={header_name: header})
    assert updated.status_code == 200, updated.text
    assert updated.json()["description"] == patch["description"]

    header = _sign_from_the_spec_alone(spec, owner, "heartbeat-listing", listing["id"], None)
    beat = client.post(f"/listings/{listing['id']}/heartbeat", headers={header_name: header})
    assert beat.status_code == 200, beat.text


def test_non_ascii_bodies_hash_as_the_spec_says() -> None:
    spec = _published_spec()
    owner = Account.create()
    listing = client.post("/listings", json=listing_payload("offering", owner.address)).json()
    patch = {"description": "café — naïve 日本語"}
    header = _sign_from_the_spec_alone(spec, owner, "update-listing", listing["id"], patch)
    updated = client.patch(f"/listings/{listing['id']}", json=patch, headers={"X-Wallet-Auth": header})
    assert updated.status_code == 200, updated.text


def test_the_examples_signer_is_a_reserved_address() -> None:
    from app.core.reserved import is_reserved_address

    assert is_reserved_address(spec_module.EXAMPLE_ADDRESS)
