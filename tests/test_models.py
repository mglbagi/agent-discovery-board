import pytest
from pydantic import ValidationError

from app.core.models import ListingCreate, ListingUpdate, check_pricing_consistency

VALID_ADDRESS = "0x1234567890123456789012345678901234567890"
OTHER_ADDRESS = "0xabcdefABCDEF00000000000000000000000000AB"


def _base(**overrides) -> dict:
    data = {
        "name": "Example Extraction Agent",
        "description": "Extracts structured line items from PDF invoices.",
        "listing_type": "offering",
        "task_categories": ["data extraction"],
        "endpoint_url": "https://example.com/agents/invoice-extractor",
        "payment_wallet": VALID_ADDRESS,
        "submitted_by": VALID_ADDRESS,
    }
    data.update(overrides)
    return data


def test_minimal_valid_listing_parses() -> None:
    listing = ListingCreate(**_base())
    assert listing.listing_type == "offering"
    assert listing.task_categories == ["data extraction"]
    assert listing.verification_agent_id is None


def test_listing_type_is_lowercased() -> None:
    listing = ListingCreate(**_base(listing_type="Offering"))
    assert listing.listing_type == "offering"


@pytest.mark.parametrize("bad_type", ["", "1starts-with-digit", "has space", "x" * 51, "bad!char"])
def test_invalid_listing_type_rejected(bad_type: str) -> None:
    with pytest.raises(ValidationError):
        ListingCreate(**_base(listing_type=bad_type))


def test_novel_listing_type_is_accepted() -> None:
    # listing_type is open/extensible: anything shaped like a slug is fine, not just
    # the four documented starting values.
    listing = ListingCreate(**_base(listing_type="collaboration-offer"))
    assert listing.listing_type == "collaboration-offer"


def test_unknown_task_category_rejected() -> None:
    with pytest.raises(ValidationError):
        ListingCreate(**_base(task_categories=["quantum-flux-analysis"]))


def test_empty_task_categories_rejected() -> None:
    with pytest.raises(ValidationError):
        ListingCreate(**_base(task_categories=[]))


def test_duplicate_task_categories_rejected() -> None:
    with pytest.raises(ValidationError):
        ListingCreate(**_base(task_categories=["other", "other"]))


def test_multiple_task_categories_allowed() -> None:
    listing = ListingCreate(**_base(task_categories=["data extraction", "data validation"]))
    assert set(listing.task_categories) == {"data extraction", "data validation"}


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://example.com/insecure",  # not https
        "ftp://example.com/agent",
        "not-a-url-at-all",
        "https://",
        "javascript:alert(1)",
    ],
)
def test_non_https_or_malformed_endpoint_url_rejected(bad_url: str) -> None:
    with pytest.raises(ValidationError):
        ListingCreate(**_base(endpoint_url=bad_url))


def test_endpoint_url_too_long_rejected() -> None:
    huge = "https://example.com/" + "a" * 2048
    with pytest.raises(ValidationError):
        ListingCreate(**_base(endpoint_url=huge))


@pytest.mark.parametrize(
    "bad_address",
    [
        "0x123",  # too short
        "1234567890123456789012345678901234567890",  # missing 0x
        "0x123456789012345678901234567890123456789g",  # non-hex char
        "0X1234567890123456789012345678901234567890",  # uppercase 0X prefix rejected by regex
        "",
    ],
)
def test_invalid_wallet_addresses_rejected(bad_address: str) -> None:
    with pytest.raises(ValidationError):
        ListingCreate(**_base(payment_wallet=bad_address))
    with pytest.raises(ValidationError):
        ListingCreate(**_base(submitted_by=bad_address))


def test_control_characters_in_name_rejected() -> None:
    with pytest.raises(ValidationError):
        ListingCreate(**_base(name="Bad\x00Name"))


def test_control_characters_in_description_rejected() -> None:
    with pytest.raises(ValidationError):
        ListingCreate(**_base(description="line1\x01line2"))


def test_newlines_in_description_are_fine() -> None:
    # Only control chars are rejected, not ordinary multi-line text.
    listing = ListingCreate(**_base(description="line one\nline two"))
    assert "\n" in listing.description


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        ListingCreate(**_base(status="active"))  # status isn't settable on create


# ---- pricing consistency ------------------------------------------------------------


@pytest.mark.parametrize("listing_type", ["announcement", "notice"])
def test_pricing_fields_rejected_for_non_priceable_types(listing_type: str) -> None:
    with pytest.raises(ValidationError):
        ListingCreate(
            **_base(listing_type=listing_type, pricing_model="per_call", pricing_amount="$0.05")
        )


@pytest.mark.parametrize("listing_type", ["announcement", "notice"])
def test_no_pricing_fields_is_fine_for_non_priceable_types(listing_type: str) -> None:
    listing = ListingCreate(**_base(listing_type=listing_type))
    assert listing.pricing_model is None
    assert listing.pricing_amount is None


def test_free_pricing_model_forbids_amount() -> None:
    with pytest.raises(ValidationError):
        ListingCreate(**_base(pricing_model="free", pricing_amount="$0.00"))


def test_free_pricing_model_without_amount_is_fine() -> None:
    listing = ListingCreate(**_base(pricing_model="free"))
    assert listing.pricing_model == "free"


def test_paid_pricing_model_requires_amount() -> None:
    with pytest.raises(ValidationError):
        ListingCreate(**_base(pricing_model="per_call"))


def test_amount_without_model_rejected() -> None:
    with pytest.raises(ValidationError):
        ListingCreate(**_base(pricing_amount="$0.05"))


def test_offering_with_full_pricing_is_valid() -> None:
    listing = ListingCreate(**_base(pricing_model="per_call", pricing_amount="$0.05"))
    assert listing.pricing_model == "per_call"
    assert listing.pricing_amount == "$0.05"


def test_check_pricing_consistency_used_directly_by_patch_route() -> None:
    check_pricing_consistency("offering", "per_call", "$0.05")  # does not raise
    with pytest.raises(ValueError):
        check_pricing_consistency("notice", "per_call", "$0.05")


# ---- ListingUpdate --------------------------------------------------------------


def test_update_allows_partial_fields() -> None:
    update = ListingUpdate(description="A new, better description.")
    dumped = update.model_dump(exclude_unset=True)
    assert dumped == {"description": "A new, better description."}


def test_update_rejects_immutable_fields() -> None:
    with pytest.raises(ValidationError):
        ListingUpdate(submitted_by=OTHER_ADDRESS)
    with pytest.raises(ValidationError):
        ListingUpdate(id="something")
    with pytest.raises(ValidationError):
        ListingUpdate(created_at="2026-01-01T00:00:00Z")


def test_update_can_set_status() -> None:
    update = ListingUpdate(status="inactive")
    assert update.model_dump(exclude_unset=True) == {"status": "inactive"}


def test_update_validates_fields_same_as_create() -> None:
    with pytest.raises(ValidationError):
        ListingUpdate(endpoint_url="http://not-https.example.com")
    with pytest.raises(ValidationError):
        ListingUpdate(task_categories=["not-a-real-category"])
