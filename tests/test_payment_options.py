"""payment_options: structured, per-network validated ways to pay; payment_wallet stays
for backward compatibility and is marked deprecated."""

import pytest
from eth_account import Account
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.models import ListingCreate, ListingUpdate, PaymentOption, is_base58_pubkey
from app.main import app
from tests.helpers import assert_error, listing_payload, wallet_auth_header

client = TestClient(app)

BASE = "eip155:8453"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_WALLET = "38Fmaf3MWTR6AWPWrtrdXoqn6iqfVcUBHMFRhiUAEjFb"
EVM_WALLET = "0x42a3c399f83BCcC3b9eEf81e65954a715D54855E"


def _option(**overrides) -> dict:
    option = {"network": BASE, "asset": BASE_USDC, "pay_to": EVM_WALLET, "amount": "0.02", "unit": "per_verification"}
    option.update(overrides)
    return option


def _solana(**overrides) -> dict:
    return _option(**{"network": SOLANA, "asset": SOLANA_USDC, "pay_to": SOLANA_WALLET, **overrides})


# ---- model validation -----------------------------------------------------------------------


def test_valid_evm_and_solana_options_are_accepted() -> None:
    assert PaymentOption(**_option()).network == BASE
    assert PaymentOption(**_solana()).pay_to == SOLANA_WALLET


def test_amount_and_unit_are_optional() -> None:
    option = PaymentOption(network=BASE, asset=BASE_USDC, pay_to=EVM_WALLET)
    assert option.amount is None and option.unit is None


@pytest.mark.parametrize(
    "bad_address",
    [
        "0x123",  # too short
        EVM_WALLET[2:],  # no 0x
        "0x" + "g" * 40,  # not hex
        EVM_WALLET + "00",  # too long
        SOLANA_WALLET,  # a Solana address on an EVM network
        "",
    ],
)
def test_invalid_evm_addresses_are_rejected_for_pay_to_and_asset(bad_address: str) -> None:
    with pytest.raises(ValidationError):
        PaymentOption(**_option(pay_to=bad_address))
    with pytest.raises(ValidationError):
        PaymentOption(**_option(asset=bad_address))


@pytest.mark.parametrize(
    "bad_address",
    [
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt10",  # '0' is not base58
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDtIl",  # 'I' and 'l' are not base58
        "abc",  # far too short
        "A" * 32,  # valid alphabet, decodes to 24 bytes
        "z" * 44,  # valid alphabet, decodes to 33 bytes
        EVM_WALLET,  # an EVM address on a Solana network
        "",
    ],
)
def test_invalid_solana_addresses_are_rejected_for_pay_to_and_asset(bad_address: str) -> None:
    with pytest.raises(ValidationError):
        PaymentOption(**_solana(pay_to=bad_address))
    with pytest.raises(ValidationError):
        PaymentOption(**_solana(asset=bad_address))


def test_base58_helper_boundaries() -> None:
    assert is_base58_pubkey("1" * 32)  # 32 zero bytes (the system program)
    assert is_base58_pubkey(SOLANA_USDC) and is_base58_pubkey(SOLANA_WALLET)
    assert not is_base58_pubkey("1" * 31) and not is_base58_pubkey("1" * 33)


@pytest.mark.parametrize(
    "network",
    [
        "cosmos:cosmoshub-4",  # a real CAIP-2 id, but a namespace this board does not validate
        "base",  # not CAIP-2 at all
        "EIP155:8453",  # namespace must be lowercase
        "eip155:0",  # chain ids start at 1
        "eip155:abc",
        "eip155:",
        "solana:short",  # reference must be 32 base58 characters
        "solana:" + "0" * 32,  # '0' is not base58
        "",
    ],
)
def test_bad_or_unsupported_networks_are_rejected(network: str) -> None:
    with pytest.raises(ValidationError):
        PaymentOption(**_option(network=network))


def test_the_unsupported_namespace_error_says_what_is_supported() -> None:
    with pytest.raises(ValidationError) as exc:
        PaymentOption(**_option(network="cosmos:cosmoshub-4"))
    assert "eip155" in str(exc.value) and "solana" in str(exc.value)


@pytest.mark.parametrize("bad", ["1e5", "-1", "0.0000000000000000001", "01", ".5", "1,5", "free", ""])
def test_bad_amounts_are_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        PaymentOption(**_option(amount=bad))


@pytest.mark.parametrize("good", ["0", "0.02", "10", "1.000000000000000001"])
def test_good_amounts_are_accepted(good: str) -> None:
    assert PaymentOption(**_option(amount=good)).amount == good


def test_unit_must_be_a_slug_and_needs_an_amount() -> None:
    with pytest.raises(ValidationError):
        PaymentOption(**_option(unit="Per Call"))
    with pytest.raises(ValidationError):
        PaymentOption(network=BASE, asset=BASE_USDC, pay_to=EVM_WALLET, unit="per_call")


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        PaymentOption(**_option(memo="hi"))


def test_at_most_twenty_options() -> None:
    base = listing_payload("offering", EVM_WALLET)
    ListingCreate(**{**base, "payment_options": [_option()] * 20})
    with pytest.raises(ValidationError):
        ListingCreate(**{**base, "payment_options": [_option()] * 21})


def test_options_are_not_applicable_to_announcements_and_notices() -> None:
    for listing_type in ("announcement", "notice"):
        with pytest.raises(ValidationError):
            ListingCreate(**listing_payload(listing_type, EVM_WALLET, payment_options=[_option()]))
        ListingCreate(**listing_payload(listing_type, EVM_WALLET, payment_options=[]))  # empty is fine


def test_an_update_cannot_send_null_options() -> None:
    with pytest.raises(ValidationError):
        ListingUpdate(payment_options=None)
    assert ListingUpdate(payment_options=[]).payment_options == []
    assert "payment_options" not in ListingUpdate().model_dump(exclude_unset=True)


# ---- through the API ------------------------------------------------------------------------


def _create(**overrides):
    owner = Account.create()
    response = client.post("/listings", json=listing_payload("offering", owner.address, **overrides))
    return owner, response


def test_options_round_trip_through_create_and_get() -> None:
    options = [_option(), _solana(amount="0.02", unit="per_verification")]
    owner, response = _create(payment_options=options)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["payment_options"] == options
    assert client.get(f"/listings/{created['id']}").json()["payment_options"] == options


def test_omitting_options_yields_an_empty_list_and_payment_wallet_still_works() -> None:
    owner, response = _create()
    assert response.status_code == 201
    body = response.json()
    assert body["payment_options"] == []
    assert body["payment_wallet"] == owner.address  # legacy field is untouched


def test_payment_wallet_is_still_required() -> None:
    owner = Account.create()
    payload = listing_payload("offering", owner.address)
    del payload["payment_wallet"]
    assert_error(client.post("/listings", json=payload), 422, "validation_error")


def test_an_invalid_option_is_a_coded_422_naming_the_field() -> None:
    owner, response = _create(payment_options=[_option(pay_to="0x123")])
    body = assert_error(response, 422, "validation_error")
    assert "pay_to" in str(body["detail"])


def test_a_solana_address_on_an_evm_network_is_rejected_through_the_api() -> None:
    owner, response = _create(payment_options=[_option(pay_to=SOLANA_WALLET)])
    assert_error(response, 422, "validation_error")


def test_patch_replaces_and_clears_options() -> None:
    owner, response = _create(payment_options=[_option()])
    listing = response.json()

    replace = {"payment_options": [_solana()]}
    header = wallet_auth_header(owner, action="update-listing", listing_id=listing["id"], body=replace)
    updated = client.patch(f"/listings/{listing['id']}", json=replace, headers={"X-Wallet-Auth": header})
    assert updated.status_code == 200, updated.text
    assert [o["network"] for o in updated.json()["payment_options"]] == [SOLANA]

    clear = {"payment_options": []}
    header = wallet_auth_header(owner, action="update-listing", listing_id=listing["id"], body=clear)
    cleared = client.patch(f"/listings/{listing['id']}", json=clear, headers={"X-Wallet-Auth": header})
    assert cleared.json()["payment_options"] == []


def test_patch_with_null_options_is_rejected() -> None:
    owner, response = _create(payment_options=[_option()])
    listing = response.json()
    patch = {"payment_options": None}
    header = wallet_auth_header(owner, action="update-listing", listing_id=listing["id"], body=patch)
    result = client.patch(f"/listings/{listing['id']}", json=patch, headers={"X-Wallet-Auth": header})
    assert_error(result, 422, "validation_error")


def test_patch_cannot_turn_a_listing_with_options_into_an_announcement() -> None:
    owner, response = _create(payment_options=[_option()])
    listing = response.json()
    patch = {"listing_type": "notice", "pricing_model": "free"}  # merged view still has payment_options
    header = wallet_auth_header(owner, action="update-listing", listing_id=listing["id"], body=patch)
    assert client.patch(f"/listings/{listing['id']}", json=patch, headers={"X-Wallet-Auth": header}).status_code == 422


# ---- deprecation is published ----------------------------------------------------------------


def test_payment_wallet_is_marked_deprecated_in_openapi_and_the_manifest() -> None:
    spec = client.get("/openapi.json").json()["components"]["schemas"]
    for model in ("ListingCreate", "ListingResponse", "ListingUpdate"):
        assert spec[model]["properties"]["payment_wallet"]["deprecated"] is True, model
    assert "payment_options" in spec["ListingCreate"]["properties"]

    params = client.get("/.well-known/agent-card.json").json()["capabilities"]["extensions"][0]["params"]
    assert params["inputSchema"]["properties"]["payment_wallet"]["deprecated"] is True
    assert "payment_wallet" in params["deprecatedFields"]
    assert params["paymentOptions"]["shape"]["properties"]["network"]
    assert ListingCreate(**{**listing_payload("offering", EVM_WALLET), **params["example"]}).payment_options
