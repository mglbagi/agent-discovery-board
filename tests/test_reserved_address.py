"""Addresses whose private keys are public can never own a listing (422 reserved_address)."""

import importlib.util
from pathlib import Path

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app.core import signing_spec
from app.core.errors import ERROR_CODES
from app.core.reserved import HARDHAT_ACCOUNT_0, RESERVED_ADDRESSES, is_reserved_address
from app.main import app
from tests.helpers import assert_error, listing_payload

client = TestClient(app)

VARIANTS = [
    HARDHAT_ACCOUNT_0,
    HARDHAT_ACCOUNT_0.lower(),
    "0x" + HARDHAT_ACCOUNT_0[2:].upper(),
]


def test_the_hardhat_zero_address_is_the_manifest_example_signer() -> None:
    assert HARDHAT_ACCOUNT_0 == signing_spec.EXAMPLE_ADDRESS == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    assert Account.from_key(signing_spec.EXAMPLE_PRIVATE_KEY).address == HARDHAT_ACCOUNT_0
    assert HARDHAT_ACCOUNT_0 in RESERVED_ADDRESSES


@pytest.mark.parametrize("address", VARIANTS)
def test_the_reserved_address_is_refused_as_submitted_by_in_any_case(address: str) -> None:
    response = client.post("/listings", json=listing_payload("offering", address))
    body = assert_error(response, 422, "reserved_address")
    assert "private key is public" in body["message"]
    retry = next(a for a in body["next_actions"] if a["method"] == "POST")
    assert retry["path"] == "/listings" and retry["required_fields"] == ["submitted_by"]


@pytest.mark.parametrize("listing_type", ["offering", "request", "announcement", "notice"])
def test_it_is_refused_for_every_listing_type(listing_type: str) -> None:
    response = client.post("/listings", json=listing_payload(listing_type, HARDHAT_ACCOUNT_0))
    assert_error(response, 422, "reserved_address")


def test_a_refused_post_creates_nothing() -> None:
    name = "should never exist reserved-check"
    assert_error(client.post("/listings", json=listing_payload("offering", HARDHAT_ACCOUNT_0, name=name)), 422, "reserved_address")
    assert client.get("/listings", params={"q": name}).json()["total"] == 0
    assert client.get("/listings", params={"q": name, "status": "inactive"}).json()["total"] == 0


def test_the_rule_is_about_ownership_so_payment_wallet_is_not_affected() -> None:
    owner = Account.create()
    payload = listing_payload("offering", owner.address, payment_wallet=HARDHAT_ACCOUNT_0)
    assert client.post("/listings", json=payload).status_code == 201


def test_ordinary_addresses_are_unaffected() -> None:
    owner = Account.create()
    assert not is_reserved_address(owner.address)
    assert client.post("/listings", json=listing_payload("offering", owner.address)).status_code == 201


def test_a_malformed_lookalike_is_still_a_plain_validation_error() -> None:
    response = client.post("/listings", json=listing_payload("offering", HARDHAT_ACCOUNT_0[:-1]))
    assert_error(response, 422, "validation_error")


def test_the_error_code_and_reserved_list_are_published() -> None:
    assert ERROR_CODES["reserved_address"].http_status == 422
    params = client.get("/.well-known/agent-card.json").json()["capabilities"]["extensions"][0]["params"]
    assert params["reservedAddresses"]["addresses"] == list(RESERVED_ADDRESSES)
    assert "reserved_address" in {c["error_code"] for c in params["errors"]["codes"]}
    assert "reserved_address" in client.get("/llms.txt").text
    assert "`reserved_address`" in client.get("/openapi.json").json()["info"]["description"]


def test_the_operator_script_also_refuses_it(capsys, tmp_path) -> None:
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("admin_update_listing", root / "scripts" / "admin_update_listing.py")
    admin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admin)

    owner = Account.create()
    listing = client.post("/listings", json=listing_payload("offering", owner.address)).json()
    code = admin.main(
        [
            "--id", listing["id"], "--expect", f"submitted_by={owner.address}",
            "--set", f"submitted_by={HARDHAT_ACCOUNT_0}", "--apply", "--yes", "--log-file", str(tmp_path / "log"),
        ]
    )
    assert code == 1 and "reserved_address" in capsys.readouterr().err
    assert client.get(f"/listings/{listing['id']}").json()["submitted_by"] == owner.address
