"""Request/response models for a listing.

Two validation styles on purpose, mirroring how the spec itself treats these
fields differently:
  * task_categories is a FIXED, closed list — anything outside it is a 422.
  * listing_type is OPEN and extensible — validated only for basic hygiene
    (a short lowercase slug), never rejected just for being an unfamiliar
    value. KNOWN_LISTING_TYPES documents the starting set; it is not an
    allowlist.
"""

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.constants import PRICING_NOT_APPLICABLE_TYPES, TASK_CATEGORIES

NAME_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 2000
PRICING_AMOUNT_MAX_LENGTH = 100
ERC8004_IDENTITY_MAX_LENGTH = 500
VERIFICATION_AGENT_ID_MAX_LENGTH = 200
ENDPOINT_URL_MAX_LENGTH = 2048

# Excludes tab/newline/CR (0x09, 0x0a, 0x0d) so ordinary multi-line text is fine;
# blocks null bytes and other control characters, which have no legitimate use in
# a name/description/identity string and are basic data hygiene for a public
# directory (not a policy judgment call, so unlike the verification service's
# opt-in strict_content_check, this is always on).
_CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_LISTING_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,49}$")


def _reject_control_chars(value: str, field: str) -> str:
    if _CONTROL_CHARS.search(value):
        raise ValueError(f"{field} contains control characters, which are not allowed")
    return value


def _validate_task_categories(value: list[str]) -> list[str]:
    if not value:
        raise ValueError("task_categories must include at least one category")
    if len(value) != len(set(value)):
        raise ValueError("task_categories must not contain duplicates")
    unknown = [c for c in value if c not in TASK_CATEGORIES]
    if unknown:
        raise ValueError(
            f"unknown task_categories {unknown!r}; must be one of {list(TASK_CATEGORIES)}"
        )
    return value


def _validate_listing_type(value: str) -> str:
    normalized = value.strip().lower()
    if not _LISTING_TYPE_RE.match(normalized):
        raise ValueError(
            "listing_type must be a lowercase slug (letters, digits, '-', '_'; starting "
            "with a letter; 1-50 characters) — e.g. 'offering', 'request', 'announcement', "
            "'notice', or a new value of your own"
        )
    return normalized


def _validate_https_url(value: str) -> str:
    from urllib.parse import urlparse

    if len(value) > ENDPOINT_URL_MAX_LENGTH:
        raise ValueError(f"endpoint_url must be at most {ENDPOINT_URL_MAX_LENGTH} characters")
    if _CONTROL_CHARS.search(value):
        raise ValueError("endpoint_url contains control characters, which are not allowed")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        # https-only, no exceptions: a lesson learned the hard way on the sibling
        # service's own listing in the CDP Bazaar, which silently declines to index
        # any resource.url that isn't HTTPS.
        raise ValueError("endpoint_url must be an https:// URL")
    return value


def _validate_address(value: str, field: str) -> str:
    if not _ADDRESS_RE.match(value):
        raise ValueError(f"{field} must be a 0x-prefixed 40-hex-character Ethereum address")
    return value


Name = Annotated[str, Field(min_length=1, max_length=NAME_MAX_LENGTH)]
Description = Annotated[str, Field(min_length=1, max_length=DESCRIPTION_MAX_LENGTH)]
PricingAmount = Annotated[str, Field(min_length=1, max_length=PRICING_AMOUNT_MAX_LENGTH)]
Erc8004Identity = Annotated[str, Field(min_length=1, max_length=ERC8004_IDENTITY_MAX_LENGTH)]
VerificationAgentId = Annotated[str, Field(min_length=1, max_length=VERIFICATION_AGENT_ID_MAX_LENGTH)]

PricingModel = Literal["free", "per_call", "subscription", "other"]
Status = Literal["active", "inactive"]


def check_pricing_consistency(listing_type: str, pricing_model: str | None, pricing_amount: str | None) -> None:
    """Also called by the PATCH route (app/api/routes/listings.py) against the
    MERGED view of an update, since a partial patch alone doesn't show the whole
    resulting state."""
    if listing_type in PRICING_NOT_APPLICABLE_TYPES:
        if pricing_model is not None or pricing_amount is not None:
            raise ValueError(
                f"pricing_model/pricing_amount are not applicable to listing_type "
                f"{listing_type!r} and must be omitted"
            )
        return
    if pricing_model == "free" and pricing_amount is not None:
        raise ValueError("pricing_amount must be omitted when pricing_model is 'free'")
    if pricing_model is not None and pricing_model != "free" and pricing_amount is None:
        raise ValueError(f"pricing_amount is required when pricing_model is {pricing_model!r}")
    if pricing_amount is not None and pricing_model is None:
        raise ValueError("pricing_amount requires pricing_model to be set")


class ListingCreate(BaseModel):
    model_config = {"extra": "forbid"}

    name: Name
    description: Description
    listing_type: str = Field(
        description="Open, extensible category. Documented starting set: offering, request, "
        "announcement, notice — but any lowercase-slug value is accepted."
    )
    task_categories: list[str] = Field(
        min_length=1,
        max_length=len(TASK_CATEGORIES),
        description=f"One or more of: {list(TASK_CATEGORIES)}.",
    )
    endpoint_url: str = Field(description="Where to actually reach this service/agent. Must be https://.")
    payment_wallet: str = Field(description="0x-prefixed Ethereum address that receives payment for this listing.")
    pricing_model: PricingModel | None = None
    pricing_amount: PricingAmount | None = None
    erc8004_identity: Erc8004Identity | None = None
    verification_agent_id: VerificationAgentId | None = Field(
        default=None,
        description="agent_id to look up on the verification service for this listing's trust-score "
        "badge (see GET /score/{agent_id} on the verification service). Defaults to submitted_by.",
    )
    submitted_by: str = Field(description="0x-prefixed Ethereum address of the submitter; who future edits/deletes must be signed by.")

    _clean_name = field_validator("name")(lambda v: _reject_control_chars(v, "name"))
    _clean_description = field_validator("description")(lambda v: _reject_control_chars(v, "description"))
    _clean_listing_type = field_validator("listing_type")(lambda v: _validate_listing_type(v))
    _clean_categories = field_validator("task_categories")(lambda v: _validate_task_categories(v))
    _clean_url = field_validator("endpoint_url")(lambda v: _validate_https_url(v))
    _clean_wallet = field_validator("payment_wallet")(lambda v: _validate_address(v, "payment_wallet"))
    _clean_submitter = field_validator("submitted_by")(lambda v: _validate_address(v, "submitted_by"))
    _clean_erc8004 = field_validator("erc8004_identity")(
        lambda v: _reject_control_chars(v, "erc8004_identity") if v is not None else v
    )
    _clean_agent_id = field_validator("verification_agent_id")(
        lambda v: _reject_control_chars(v, "verification_agent_id") if v is not None else v
    )

    @model_validator(mode="after")
    def _pricing(self) -> "ListingCreate":
        check_pricing_consistency(self.listing_type, self.pricing_model, self.pricing_amount)
        return self


class ListingUpdate(BaseModel):
    """PATCH: every field optional; only fields explicitly present in the request are
    applied. Deliberately has no id, submitted_by, or created_at fields at all, so
    there is no way to even attempt changing them through this model."""

    model_config = {"extra": "forbid"}

    name: Name | None = None
    description: Description | None = None
    listing_type: str | None = None
    task_categories: list[str] | None = Field(default=None, max_length=len(TASK_CATEGORIES))
    endpoint_url: str | None = None
    payment_wallet: str | None = None
    pricing_model: PricingModel | None = None
    pricing_amount: PricingAmount | None = None
    erc8004_identity: Erc8004Identity | None = None
    verification_agent_id: VerificationAgentId | None = None
    status: Status | None = None

    _clean_name = field_validator("name")(lambda v: _reject_control_chars(v, "name") if v is not None else v)
    _clean_description = field_validator("description")(
        lambda v: _reject_control_chars(v, "description") if v is not None else v
    )
    _clean_listing_type = field_validator("listing_type")(
        lambda v: _validate_listing_type(v) if v is not None else v
    )
    _clean_categories = field_validator("task_categories")(
        lambda v: _validate_task_categories(v) if v is not None else v
    )
    _clean_url = field_validator("endpoint_url")(lambda v: _validate_https_url(v) if v is not None else v)
    _clean_wallet = field_validator("payment_wallet")(
        lambda v: _validate_address(v, "payment_wallet") if v is not None else v
    )
    _clean_erc8004 = field_validator("erc8004_identity")(
        lambda v: _reject_control_chars(v, "erc8004_identity") if v is not None else v
    )
    _clean_agent_id = field_validator("verification_agent_id")(
        lambda v: _reject_control_chars(v, "verification_agent_id") if v is not None else v
    )


class Badge(BaseModel):
    """Attached to a listing when a trust-score lookup was actually performed (whether
    or not it found anything). Entirely absent (the listing's `badge` field is null)
    when no lookup was attempted at all — see app/core/score_client.py."""

    trust_score: float | None
    confidence_interval_95: list[float] | None
    sample_size: int
    identity_verified: bool | None
    reason: str | None
    fetched_at: str
    source: str


class ListingResponse(BaseModel):
    id: str
    name: str
    description: str
    listing_type: str
    task_categories: list[str]
    endpoint_url: str
    payment_wallet: str
    pricing_model: str | None
    pricing_amount: str | None
    erc8004_identity: str | None
    verification_agent_id: str | None
    submitted_by: str
    status: str
    created_at: datetime
    updated_at: datetime
    badge: Badge | None = Field(
        default=None,
        description="A live trust-score lookup for this listing, when one was performed and the "
        "verification service is configured. Null whenever no lookup happened — including "
        "while badge lookups are unconfigured, which is this service's shipped default. Never "
        "required, never blocks any operation on this listing.",
    )


class ListingsPage(BaseModel):
    listings: list[ListingResponse]
    total: int
    limit: int
    offset: int
