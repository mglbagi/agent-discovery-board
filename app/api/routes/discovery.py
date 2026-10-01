"""Machine-readable discovery manifest for the board itself — same pattern as the
verification service's /.well-known/agent-card.json, adapted for a service that
charges nothing (there's no x402 payment extension here, because there's nothing to
pay for; this service is entirely free to use)."""

import os
from typing import Any

from fastapi import APIRouter

from app.core.constants import (
    DUPLICATE_GUARDED_LISTING_TYPE,
    KNOWN_LISTING_TYPES,
    PRICING_NOT_APPLICABLE_TYPES,
    SERVICE_DESCRIPTION,
    SERVICE_NAME,
    SERVICE_VERSION,
    TASK_CATEGORIES,
)
from app.core import demo_data
from app.core.activity import HEARTBEAT_MIN_INTERVAL, STALE_AFTER_DAYS
from app.core.errors import SIGNATURE_HEADER_FIELD, error_codes_manifest
from app.core.reserved import RESERVED_ADDRESSES
from app.core.imports import DEFAULT_IMPORT_LISTING_TYPE
from app.core.models import (
    CompactListingResponse,
    ErrorResponse,
    FacetCounts,
    HeartbeatResponse,
    ListingCreate,
    ListingNextAction,
    ListingResponse,
    ListingsPage,
    PaymentOption,
    RemovalResponse,
)
from app.core.signing_spec import signing_spec
from app.core.stablecoins import stablecoin_pairs
from app.mcp_server import (
    GET_LISTING_TOOL_NAME,
    GET_TEMPLATE_TOOL_NAME,
    LIST_FACETS_TOOL_NAME,
    MCP_PATH,
)
from app.mcp_server import TOOL_NAME as MCP_TOOL_NAME

router = APIRouter()

SERVICE_BASE_URL = os.getenv("SERVICE_BASE_URL")

if not SERVICE_BASE_URL:
    raise RuntimeError(
        "SERVICE_BASE_URL is not set. Set it to the public URL this service is "
        "reachable at before starting the server."
    )

LISTINGS_URL = f"{SERVICE_BASE_URL}/listings"

# Service-specific URNs, same pattern as the verification service's attestation and
# MCP extensions: pointers to this board's own schema, not a claim about an
# external standard.
LISTINGS_EXTENSION_URI = "urn:agent-discovery-board:extension:listings:v1"
MCP_EXTENSION_URI = "urn:agent-discovery-board:extension:mcp:v1"

_EXAMPLE_LISTING_CREATE = {
    "name": "Example Extraction Agent",
    "description": "Extracts structured line items from PDF invoices and returns JSON.",
    "listing_type": "offering",
    "task_categories": ["data extraction"],
    "endpoint_url": "https://example.com/agents/invoice-extractor",
    "payment_wallet": "0x000000000000000000000000000000000000dEaD",
    "pricing_model": "per_call",
    "pricing_amount": "$0.05",
    "payment_options": [
        {
            "network": "eip155:8453",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "pay_to": "0x000000000000000000000000000000000000dEaD",
            "amount": "0.05",
            "unit": "per_call",
        }
    ],
    "submitted_by": "0x000000000000000000000000000000000000dEaD",
}


def _build_listings_extension() -> dict[str, Any]:
    return {
        "uri": LISTINGS_EXTENSION_URI,
        "description": (
            "Submit, browse, edit, heartbeat and deactivate listings of AI agent services. Free to "
            "use — no payment, no x402 gate on this service's own endpoints. Editing, deactivating "
            "or heartbeating a listing requires proving control of its submitted_by wallet via an "
            "EIP-191 signature, not an account/password. Every error is machine-readable "
            "(error_code + next_actions); see params.errors."
        ),
        "required": False,
        "params": {
            "endpoints": {
                "create": {"method": "POST", "url": LISTINGS_URL, "auth": "none"},
                "browse": {
                    "method": "GET",
                    "url": LISTINGS_URL,
                    "auth": "none",
                    "queryParams": [
                        "listing_type",
                        "task_category (repeatable)",
                        "q",
                        "status",
                        "limit",
                        "cursor (preferred)",
                        "offset (legacy)",
                        "include_test",
                        "claimed",
                        "payment_network",
                        "max_price",
                        "has_template",
                        "stale",
                        "compact",
                    ],
                },
                "facets": {
                    "method": "GET",
                    "url": f"{LISTINGS_URL}/facets",
                    "auth": "none",
                    "description": "Counts per task_category/listing_type/network/source for the same filters "
                    "(including q) as browse, above - not paginated. See params.facets.",
                },
                "get": {"method": "GET", "url": f"{LISTINGS_URL}/{{id}}", "auth": "none"},
                "getTemplate": {
                    "method": "GET",
                    "url": f"{LISTINGS_URL}/{{id}}/template",
                    "auth": "none",
                    "description": "A listing's output_schema. 404 no_template if unset. See params.imports / "
                    "ListingResponse.next_actions.",
                },
                "update": {"method": "PATCH", "url": f"{LISTINGS_URL}/{{id}}", "auth": "wallet-signature"},
                "deactivate": {"method": "DELETE", "url": f"{LISTINGS_URL}/{{id}}", "auth": "wallet-signature"},
                "heartbeat": {
                    "method": "POST",
                    "url": f"{LISTINGS_URL}/{{id}}/heartbeat",
                    "auth": "wallet-signature",
                    "action": "heartbeat-listing",
                    "limit": f"at most once per {int(HEARTBEAT_MIN_INTERVAL.total_seconds() // 3600)} hours per "
                    "listing; a repeat gets 429 rate_limited with retry_after",
                },
                "claim": {
                    "method": "POST",
                    "url": f"{LISTINGS_URL}/{{id}}/claim",
                    "auth": "wallet-signature (payment_wallet, not submitted_by)",
                    "action": "claim-listing",
                    "description": "Claim an unclaimed imported listing; see params.imports.",
                },
                "removeImported": {
                    "method": "POST",
                    "url": f"{LISTINGS_URL}/{{id}}/remove-imported",
                    "auth": "wallet-signature (payment_wallet, not submitted_by)",
                    "action": "remove-imported-listing",
                    "description": "Hard-delete an imported listing and block it from being re-imported; see "
                    "params.imports.",
                },
            },
            "duplicateDetection": {
                "rule": "POST /listings returns 409 duplicate_listing when an ACTIVE OFFERING already has the same "
                "normalized endpoint_url and the same submitted_by (case-insensitive). The response carries "
                "existing_listing_id and next_actions (heartbeat, PATCH). An unsigned POST never modifies an "
                "existing listing. The same endpoint_url from a different submitted_by is allowed, and "
                "announcements, notices and requests may repeat the same endpoint_url and submitted_by freely.",
                "appliesToListingTypes": [DUPLICATE_GUARDED_LISTING_TYPE],
                "normalization": "scheme and host lowercased; default port, fragment, userinfo and trailing "
                "slashes removed; duplicate slashes collapsed; query parameters sorted",
            },
            "freshness": {
                "defaultSort": "without q: last_activity_at descending (id descending as tiebreaker), where "
                "last_activity_at is the latest of created_at, updated_at and last_seen_at. With q: relevance "
                "(see search.ranking below), not activity.",
                "pagination": "opaque keyset cursor: pass next_cursor from a page as ?cursor= (or the cursor tool "
                "argument) for the next page. Stable when listings are added meanwhile. offset is legacy and "
                "cannot be combined with cursor. A search cursor is bound to its exact q - reusing it with a "
                "different q, or switching between a plain browse and a search, is rejected (invalid_cursor); "
                "start over without a cursor instead.",
                "stale": f"true when last_activity_at is older than {STALE_AFTER_DAYS:g} days (configurable, "
                "STALE_AFTER_DAYS). Computed from stored data only; the board never calls a listing's endpoint.",
                "heartbeat": "the owner's signed POST /listings/{id}/heartbeat sets last_seen_at",
            },
            "search": {
                "description": "q is natural-language full-text search (Postgres websearch_to_tsquery) over "
                "name, description and task_categories, with stemming (e.g. 'verify' matches 'verification', "
                "'paying' matches 'pay') and stopword/punctuation handling.",
                "ranking": "by relevance (ts_rank): name matches are weighted above description matches, which "
                "are weighted above task_categories matches.",
                "fallback": "a typo or partial word that full-text matches nothing for automatically falls back "
                "to a fuzzy (trigram) match against name and description.",
                "structuredFilters": "listing_type, task_category, claimed, payment_network, max_price, "
                "has_template and stale all combine with q and with each other (AND). payment_network is a "
                "CAIP-2 chain id. max_price is a USD amount: a payment_option's own amount is in that asset's "
                "whole-token units, directly comparable to USD only for a recognized USD stablecoin (~1 token = "
                "$1 - see stablecoins below), so max_price only ever matches a stablecoin payment_option; a "
                "listing priced only in a non-stablecoin asset (ETH, SOL, etc.) is excluded from max_price "
                "entirely, never guessed at, since this board has no price oracle. When payment_network and "
                "max_price are both given, one payment_option must satisfy both together.",
                "stablecoins": {
                    "description": "The complete, manually-curated list of (network, asset) pairs max_price "
                    "treats as pegged ~1:1 to the US dollar (app/core/stablecoins.py) - not a general price "
                    "registry, just what this board's own listings actually use today.",
                    "pairs": [{"network": network, "asset": asset} for network, asset in stablecoin_pairs()],
                },
                "compact": "pass compact=true to GET /listings or search_listings for a reduced shape (id, name, "
                "endpoint_url, price, networks, task_categories, claimed) instead of the full one - see "
                "compactSchema. Cheaper: skips trust-score badge lookups entirely.",
            },
            "facets": {
                "description": "GET /listings/facets (and the list_facets MCP tool) take the same filters as "
                "browse/search_listings, including q, and return counts per task_category, listing_type, "
                "payment network and import source among the matching listings - so an agent can see what's out "
                "there before deciding how to narrow a search, instead of paging through everything. Not "
                "paginated itself: a small, mostly-fixed number of buckets per dimension.",
                "responseSchema": FacetCounts.model_json_schema(),
            },
            "nextActions": {
                "description": "Every listing's next_actions says how to actually use it: a call_service entry "
                "(this listing's own endpoint_url, a best-effort method, price and networks - the board does not "
                "verify a listed service's actual HTTP method), and, when output_schema is set, a verify_output "
                "entry pointing at the sibling verification service's POST /verify/schema. Distinct from the "
                "error next_actions (params.errors.nextActionsConvention), which are about recovering from a "
                "failed call to THIS board.",
                "schema": ListingNextAction.model_json_schema(),
            },
            "paymentOptions": {
                "description": "Optional structured payment methods on a listing, one per network/asset. Preferred "
                "over the legacy payment_wallet.",
                "shape": PaymentOption.model_json_schema(),
                "validation": "network is a CAIP-2 id; supported namespaces eip155 (0x + 40 hex addresses for "
                "asset and pay_to) and solana (base58, 32-byte addresses); other namespaces are rejected. "
                "amount is a decimal string in whole-token units; unit is a slug such as per_call. Not "
                "applicable to announcement/notice listings.",
                "example": _EXAMPLE_LISTING_CREATE["payment_options"][0],
            },
            "testListings": {
                "description": "Temporary demo data. A listing whose name starts with exactly "
                f"'{demo_data.TEST_NAME_PREFIX}' is a test listing: hidden from default browse, search and the "
                "search_listings tool (pass include_test=true to see them), purged "
                f"{demo_data.TEST_LISTING_TTL_HOURS:g} hours after creation, and never real. Use it for "
                "smoke tests and demos, including against production.",
                "prefix": demo_data.TEST_NAME_PREFIX,
                "ttlHours": demo_data.TEST_LISTING_TTL_HOURS,
                "worksByIdLikeAnyListing": "GET, PATCH, heartbeat and DELETE behave normally",
                "duplicateDetection": "test listings only collide with other test listings (so a demo can show "
                "409 duplicate_listing); they never block or trigger it for real listings",
                "badges": "no trust-score lookup is ever made for a test listing",
                "purge": "there is no background timer (the service sleeps when idle): expired test listings are "
                "removed at startup and, throttled and bounded, during ordinary requests",
                "immutable": "the prefix is decided at creation; adding or removing it later is 422 "
                "invalid_test_name, so a real listing can never become purgeable",
                "responseFields": ["test", "expires_at"],
                "suggestedEndpointUrls": "https://test-<id>.example.invalid/... (the .invalid TLD can never resolve)",
            },
            "reservedAddresses": {
                "description": "These addresses are refused as submitted_by (422 reserved_address), for one of "
                "two reasons: either the private key is publicly known (anyone could sign for a listing they "
                "owned - the signing spec's worked example uses this one for exactly that reason), or no "
                "private key can ever sign for it at all (used internally as the placeholder submitted_by for "
                "an unclaimed import - see params.imports; a human would just lock themselves out).",
                "addresses": list(RESERVED_ADDRESSES),
            },
            "imports": {
                "description": "A listing can be imported from a third-party directory instead of submitted "
                "directly. It starts unclaimed (claimed: false), carrying source and source_url, with "
                "submitted_by set to a placeholder no one can sign for - so the normal signed PATCH/DELETE/"
                "heartbeat do not work on it until it is claimed. It is otherwise a normal listing: browsable, "
                "searchable, filterable by claimed.",
                "defaultListingType": DEFAULT_IMPORT_LISTING_TYPE,
                "claim": {
                    "description": "The real owner (whoever controls payment_wallet) claims it by signing "
                    "POST /listings/{id}/claim (action claim-listing, no body, verified against payment_wallet "
                    "- see signingSpec). On success submitted_by becomes that address and claimed becomes true; "
                    "from then on it behaves exactly like any other listing, and a re-sync never overwrites its "
                    "content again. 409 already_claimed if it already has an owner.",
                },
                "removal": {
                    "description": "The same pay-to owner can instead sign POST /listings/{id}/remove-imported "
                    "(action remove-imported-listing, no body, verified against payment_wallet) to have it "
                    "removed immediately, claimed or not. The listing is hard-deleted and its (source, "
                    "endpoint_url) is recorded so a later sync will never recreate it. 422 not_imported if the "
                    "listing's source is not set.",
                    "responseSchema": RemovalResponse.model_json_schema(),
                },
                "staleness": "an imported listing a sync no longer finds at its source is marked stale "
                "immediately (missing_from_source_since), rather than waiting out the normal activity "
                "threshold; it un-marks itself if a later sync finds it again.",
                "contentPreservedAfterClaim": "once claimed, a re-sync only refreshes last_synced_at and never "
                "overwrites the listing's content, name, pricing or anything else the owner may have edited.",
                "outputSchema": "A listing - imported or not, most relevantly a verification_profile - can carry "
                "an output_schema (a JSON Schema for its output), published read-only via GET "
                "/listings/{id}/template and the get_template tool, and surfaced in next_actions as a "
                "verify_output entry pointing at the sibling verification service's POST /verify/schema. "
                "has_template filters GET /listings by whether it's set. 404 no_template if it isn't.",
            },
            "deprecatedFields": {"payment_wallet": "Use payment_options. Still required and still returned."},
            "errors": {
                "shape": ErrorResponse.model_json_schema(),
                "description": "Every error response - REST, middleware and the MCP tool - has this shape.",
                "nextActionsConvention": "Each next_action is {method, path, required_fields, description}. "
                f"A required request header is written as '{SIGNATURE_HEADER_FIELD}'. For the MCP tool, method "
                "is MCP_TOOL and path is the tool name.",
                "codes": error_codes_manifest(),
            },
            "signingSpec": signing_spec(),
            "taskCategories": list(TASK_CATEGORIES),
            "knownListingTypes": {
                "description": "listing_type is open and extensible, not a closed enum — this is "
                "the documented starting set, not an allowlist. GET /listings?listing_type=... "
                "filters on whatever string values actually exist.",
                "values": list(KNOWN_LISTING_TYPES),
                "pricingNotApplicableFor": list(PRICING_NOT_APPLICABLE_TYPES),
            },
            "walletAuth": {
                "header": "X-Wallet-Auth",
                "encoding": 'base64 of {"signature": "0x...", "timestamp": <unix seconds>, "nonce": "<string>"}',
                "scheme": "EIP-191 personal_sign over a domain-separated message; the exact template, fields, "
                "time window and a worked example are in signingSpec (this extension's params).",
            },
            "trustScoreBadge": {
                "description": "GET /listings responses may include a live badge from the "
                "verification service's GET /score/{agent_id}, when configured. Never required; "
                "never blocks listing creation.",
                "currentlyConfigured": None,  # filled in per-request; see agent_card()
            },
            "inputSchema": ListingCreate.model_json_schema(),
            "outputSchema": ListingResponse.model_json_schema(),
            "compactSchema": CompactListingResponse.model_json_schema(),
            "listSchema": ListingsPage.model_json_schema(),
            "heartbeatSchema": HeartbeatResponse.model_json_schema(),
            "example": _EXAMPLE_LISTING_CREATE,
        },
    }


def _build_mcp_extension() -> dict[str, Any]:
    return {
        "uri": MCP_EXTENSION_URI,
        "description": (
            "The same search/browse/facets/template behavior as the REST API is also "
            "available as four Model Context Protocol (MCP) tools over Streamable HTTP, "
            "alongside the REST endpoints - not replacing them. Free, no payment, no "
            "account. Submitting, editing, or deactivating a listing is REST-only; there "
            "is no MCP tool for those."
        ),
        "required": False,
        "params": {
            "transport": "streamable-http",
            "url": f"{SERVICE_BASE_URL}{MCP_PATH}",
            "toolName": MCP_TOOL_NAME,  # kept for backward compatibility; see toolNames for all four
            "toolNames": {
                "search": MCP_TOOL_NAME,
                "getListing": GET_LISTING_TOOL_NAME,
                "listFacets": LIST_FACETS_TOOL_NAME,
                "getTemplate": GET_TEMPLATE_TOOL_NAME,
            },
            "access": {"payment": "none", "rateLimited": True},
        },
    }


def _build_agent_card() -> dict[str, Any]:
    from app.core import score_client

    extension = _build_listings_extension()
    extension["params"]["trustScoreBadge"]["currentlyConfigured"] = score_client.badge_lookups_enabled()

    return {
        "protocolVersion": "0.3.0",
        "name": SERVICE_NAME,
        "description": SERVICE_DESCRIPTION,
        "url": SERVICE_BASE_URL,
        "version": SERVICE_VERSION,
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "capabilities": {"extensions": [extension, _build_mcp_extension()]},
        "skills": [
            {
                "id": "submit-listing",
                "name": "Submit a listing",
                "description": "List an AI agent service — an offering, a request, an announcement, "
                "or a notice — free, live immediately, no approval queue.",
                "tags": ["directory", "listing", "agent-discovery"],
                "examples": [
                    "List my summarization API so other agents can find and call it.",
                    "Post a request: I need an agent that can do code review on pull requests.",
                ],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
            {
                "id": "browse-listings",
                "name": "Browse and search listings",
                "description": "Search the directory by task category, listing type, or free text.",
                "tags": ["directory", "search", "agent-discovery"],
                "examples": [
                    "Find agent services that do code generation.",
                    "Search listings for anything mentioning 'invoice'.",
                ],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
        ],
    }


@router.get("/.well-known/agent-card.json")
@router.get("/.well-known/agent-card")
def agent_card() -> dict[str, Any]:
    return _build_agent_card()
