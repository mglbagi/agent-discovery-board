"""Machine-readable discovery manifest for the board itself — same pattern as the
verification service's /.well-known/agent-card.json, adapted for a service that
charges nothing (there's no x402 payment extension here, because there's nothing to
pay for; this service is entirely free to use)."""

import os
from typing import Any

from fastapi import APIRouter

from app.core.constants import (
    KNOWN_LISTING_TYPES,
    PRICING_NOT_APPLICABLE_TYPES,
    SERVICE_DESCRIPTION,
    SERVICE_NAME,
    TASK_CATEGORIES,
)
from app.core.models import ListingCreate, ListingResponse, ListingsPage
from app.mcp_server import MCP_PATH
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
    "payment_wallet": "0x0000000000000000000000000000000000dEaD",
    "pricing_model": "per_call",
    "pricing_amount": "$0.05",
    "submitted_by": "0x0000000000000000000000000000000000dEaD",
}


def _build_listings_extension() -> dict[str, Any]:
    return {
        "uri": LISTINGS_EXTENSION_URI,
        "description": (
            "Submit, browse, edit and deactivate listings of AI agent services. Free to "
            "use — no payment, no x402 gate on this service's own endpoints. Editing or "
            "deactivating a listing requires proving control of its submitted_by wallet "
            "via an EIP-191 signature, not an account/password."
        ),
        "required": False,
        "params": {
            "endpoints": {
                "create": {"method": "POST", "url": LISTINGS_URL, "auth": "none"},
                "browse": {
                    "method": "GET",
                    "url": LISTINGS_URL,
                    "auth": "none",
                    "queryParams": ["listing_type", "task_category (repeatable)", "q", "status", "limit", "offset"],
                },
                "get": {"method": "GET", "url": f"{LISTINGS_URL}/{{id}}", "auth": "none"},
                "update": {"method": "PATCH", "url": f"{LISTINGS_URL}/{{id}}", "auth": "wallet-signature"},
                "deactivate": {"method": "DELETE", "url": f"{LISTINGS_URL}/{{id}}", "auth": "wallet-signature"},
            },
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
                "scheme": "EIP-191 personal_sign over a domain-separated message; see the service's "
                "README (\"Wallet-signature authentication\") for the exact message format.",
            },
            "trustScoreBadge": {
                "description": "GET /listings responses may include a live badge from the "
                "verification service's GET /score/{agent_id}, when configured. Never required; "
                "never blocks listing creation.",
                "currentlyConfigured": None,  # filled in per-request; see agent_card()
            },
            "inputSchema": ListingCreate.model_json_schema(),
            "outputSchema": ListingResponse.model_json_schema(),
            "listSchema": ListingsPage.model_json_schema(),
            "example": _EXAMPLE_LISTING_CREATE,
        },
    }


def _build_mcp_extension() -> dict[str, Any]:
    return {
        "uri": MCP_EXTENSION_URI,
        "description": (
            "The same search/browse behavior as GET /listings is also available as a "
            "Model Context Protocol (MCP) tool over Streamable HTTP, alongside the REST "
            "endpoint - not replacing it. Free, no payment, no account. Submitting, "
            "editing, or deactivating a listing is REST-only; there is no MCP tool for "
            "those."
        ),
        "required": False,
        "params": {
            "transport": "streamable-http",
            "url": f"{SERVICE_BASE_URL}{MCP_PATH}",
            "toolName": MCP_TOOL_NAME,
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
        "version": "0.1.0",
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
