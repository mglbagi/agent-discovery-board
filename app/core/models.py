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

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_CAIP2_RE = re.compile(r"^[-a-z0-9]{3,8}:[-_a-zA-Z0-9]{1,32}$")
_EIP155_REFERENCE_RE = re.compile(r"^[1-9][0-9]{0,31}$")
_SOLANA_REFERENCE_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32}$")
_AMOUNT_RE = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]{1,18})?$")
_UNIT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,49}$")
SUPPORTED_NAMESPACES = ("eip155", "solana")
MAX_PAYMENT_OPTIONS = 20


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
    try:
        parsed.port  # noqa: B018 - raises ValueError for a non-numeric or out-of-range port
    except ValueError as exc:
        raise ValueError("endpoint_url has an invalid port") from exc
    if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
        # https-only, no exceptions: a lesson learned the hard way on the sibling
        # service's own listing in the CDP Bazaar, which silently declines to index
        # any resource.url that isn't HTTPS.
        raise ValueError("endpoint_url must be an https:// URL")
    return value


def _validate_address(value: str, field: str) -> str:
    if not _ADDRESS_RE.match(value):
        raise ValueError(f"{field} must be a 0x-prefixed 40-hex-character Ethereum address")
    return value


def is_base58_pubkey(value: str) -> bool:
    """A Solana address/mint: base58 text that decodes to exactly 32 bytes."""
    if not 32 <= len(value) <= 44 or any(c not in _BASE58_ALPHABET for c in value):
        return False
    number = 0
    for char in value:
        number = number * 58 + _BASE58_ALPHABET.index(char)
    leading_zeros = len(value) - len(value.lstrip("1"))
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return leading_zeros + len(decoded) == 32


def _validate_network_address(network: str, value: str, field: str) -> None:
    namespace = network.split(":", 1)[0]
    if namespace == "eip155":
        if not _ADDRESS_RE.match(value):
            raise ValueError(f"{field} must be a 0x-prefixed 40-hex-character address on network {network!r}")
    elif namespace == "solana":
        if not is_base58_pubkey(value):
            raise ValueError(f"{field} must be a base58 Solana address (32 bytes) on network {network!r}")


Name = Annotated[str, Field(min_length=1, max_length=NAME_MAX_LENGTH)]
Description = Annotated[str, Field(min_length=1, max_length=DESCRIPTION_MAX_LENGTH)]
PricingAmount = Annotated[str, Field(min_length=1, max_length=PRICING_AMOUNT_MAX_LENGTH)]
Erc8004Identity = Annotated[str, Field(min_length=1, max_length=ERC8004_IDENTITY_MAX_LENGTH)]
VerificationAgentId = Annotated[str, Field(min_length=1, max_length=VERIFICATION_AGENT_ID_MAX_LENGTH)]

PricingModel = Literal["free", "per_call", "subscription", "other"]
Status = Literal["active", "inactive"]


class PaymentOption(BaseModel):
    """One way to pay for a listing's service. Validated per network namespace:
    eip155:* takes 0x EVM addresses, solana:* takes base58 addresses. Other
    namespaces are rejected rather than accepted unchecked."""

    model_config = {"extra": "forbid"}

    network: str = Field(
        description="CAIP-2 chain id, e.g. 'eip155:8453' (Base) or 'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp' "
        f"(Solana mainnet). Supported namespaces: {list(SUPPORTED_NAMESPACES)}."
    )
    asset: str = Field(
        description="On-chain asset: an EVM token contract address (eip155) or a token mint address (solana)."
    )
    pay_to: str = Field(description="Address that receives payment on this network.")
    amount: str | None = Field(
        default=None,
        description="Price in whole-token units as a decimal string, e.g. '0.02' (up to 18 decimal places).",
    )
    unit: str | None = Field(
        default=None,
        description="What `amount` is charged per, as a lowercase slug, e.g. 'per_call', 'per_verification'.",
    )

    @model_validator(mode="after")
    def _check(self) -> "PaymentOption":
        if not _CAIP2_RE.match(self.network):
            raise ValueError(f"network {self.network!r} is not a CAIP-2 chain id (namespace:reference)")
        namespace, reference = self.network.split(":", 1)
        if namespace not in SUPPORTED_NAMESPACES:
            raise ValueError(f"network namespace {namespace!r} is not supported; use one of {list(SUPPORTED_NAMESPACES)}")
        reference_re = _EIP155_REFERENCE_RE if namespace == "eip155" else _SOLANA_REFERENCE_RE
        if not reference_re.match(reference):
            raise ValueError(f"network {self.network!r} has an invalid {namespace} chain reference")
        _validate_network_address(self.network, self.asset, "asset")
        _validate_network_address(self.network, self.pay_to, "pay_to")
        if self.amount is not None and not _AMOUNT_RE.match(self.amount):
            raise ValueError("amount must be a non-negative decimal string such as '0.02'")
        if self.unit is not None and not _UNIT_RE.match(self.unit):
            raise ValueError("unit must be a lowercase slug such as 'per_call'")
        if self.unit is not None and self.amount is None:
            raise ValueError("unit requires amount")
        return self


def _validate_payment_options_not_null(value: list[PaymentOption] | None) -> list[PaymentOption] | None:
    if value is None:
        raise ValueError("payment_options cannot be null; send [] to clear it")
    return value


def check_pricing_consistency(
    listing_type: str,
    pricing_model: str | None,
    pricing_amount: str | None,
    payment_options: list[Any] | None = None,
) -> None:
    """Also called by the PATCH route (app/api/routes/listings.py) against the
    MERGED view of an update, since a partial patch alone doesn't show the whole
    resulting state."""
    if listing_type in PRICING_NOT_APPLICABLE_TYPES:
        if pricing_model is not None or pricing_amount is not None or payment_options:
            raise ValueError(
                f"pricing_model/pricing_amount/payment_options are not applicable to listing_type "
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
    payment_wallet: str = Field(
        description="DEPRECATED - use payment_options. 0x-prefixed Ethereum address that receives payment for "
        "this listing; kept (and still required) for backward compatibility.",
        json_schema_extra={"deprecated": True},
    )
    pricing_model: PricingModel | None = None
    pricing_amount: PricingAmount | None = None
    payment_options: list[PaymentOption] = Field(
        default_factory=list,
        max_length=MAX_PAYMENT_OPTIONS,
        description="Structured ways to pay for this listing's service, one entry per network/asset. Optional. "
        "Not applicable to announcement/notice listings.",
    )
    erc8004_identity: Erc8004Identity | None = None
    verification_agent_id: VerificationAgentId | None = Field(
        default=None,
        description="agent_id to look up on the verification service for this listing's trust-score badge (see "
        "GET /score/{agent_id} on the verification service). Defaults to submitted_by.",
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
        check_pricing_consistency(self.listing_type, self.pricing_model, self.pricing_amount, self.payment_options)
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
    payment_wallet: str | None = Field(default=None, json_schema_extra={"deprecated": True})
    pricing_model: PricingModel | None = None
    pricing_amount: PricingAmount | None = None
    payment_options: list[PaymentOption] | None = Field(default=None, max_length=MAX_PAYMENT_OPTIONS)
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
    _clean_payment_options = field_validator("payment_options")(_validate_payment_options_not_null)


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
    payment_wallet: str = Field(
        description="DEPRECATED - use payment_options. Kept for backward compatibility.",
        json_schema_extra={"deprecated": True},
    )
    pricing_model: str | None
    pricing_amount: str | None
    payment_options: list[PaymentOption] = Field(default_factory=list)
    erc8004_identity: str | None
    verification_agent_id: str | None
    submitted_by: str
    status: str
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime | None = Field(
        default=None,
        description="When the owner last sent a signed heartbeat (POST /listings/{id}/heartbeat); null if never.",
    )
    last_activity_at: datetime = Field(
        description="The latest of created_at, updated_at and last_seen_at; the default sort key of GET /listings."
    )
    stale: bool = Field(
        description="True when last_activity_at is older than the service's staleness threshold "
        "(STALE_AFTER_DAYS, default 60). Computed from stored data only; the board never probes endpoints."
    )
    badge: Badge | None = Field(
        default=None,
        description="A live trust-score lookup for this listing, when one was performed and the "
        "verification service is configured. Null whenever no lookup happened — including "
        "while badge lookups are unconfigured, which is this service's shipped default. Never "
        "required, never blocks any operation on this listing.",
    )


class ListingsPage(BaseModel):
    listings: list[ListingResponse]
    total: int = Field(description="Number of listings matching the filters (not just this page).")
    limit: int
    offset: int = Field(description="Legacy offset paging; prefer cursor.")
    next_cursor: str | None = Field(
        default=None,
        description="Pass as ?cursor= (or the cursor tool argument) to get the next page; null on the last page.",
    )


class HeartbeatResponse(BaseModel):
    id: str
    last_seen_at: datetime
    next_heartbeat_allowed_at: datetime = Field(description="Earliest time another heartbeat will be accepted.")
    last_activity_at: datetime
    stale: bool


class NextAction(BaseModel):
    method: str = Field(description="HTTP method, or MCP_TOOL for the MCP tool.")
    path: str
    required_fields: list[str] = Field(
        description="Body/query fields to send; a required header is written 'header:<Name>'."
    )
    description: str


class ErrorResponse(BaseModel):
    """Every error response from this service (REST, middleware and MCP) has this shape."""

    error_code: str = Field(description="Stable machine-readable code; the full list is in the manifest.")
    message: str
    detail: Any = Field(description="A string for most errors; FastAPI's error list for validation_error.")
    next_actions: list[NextAction]
    retry_after: int | None = Field(default=None, description="Seconds to wait (rate_limited).")
    existing_listing_id: str | None = Field(default=None, description="duplicate_listing only.")
    server_time: int | None = Field(default=None, description="Server unix time (stale_signature only).")
    max_age_seconds: int | None = Field(default=None, description="Allowed signature age (stale_signature only).")
