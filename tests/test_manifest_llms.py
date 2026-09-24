"""The manifest, OpenAPI document and /llms.txt describe the new behavior, and are
generated from the same registries the service enforces."""

from fastapi.testclient import TestClient

from app.core import activity
from app.core.errors import ERROR_CODES
from app.core.constants import SERVICE_VERSION, TASK_CATEGORIES
from app.main import app

client = TestClient(app)


def _params() -> dict:
    return client.get("/.well-known/agent-card.json").json()["capabilities"]["extensions"][0]["params"]


def test_the_manifest_documents_the_new_endpoints_and_rules() -> None:
    params = _params()
    heartbeat = params["endpoints"]["heartbeat"]
    assert heartbeat["method"] == "POST" and heartbeat["url"].endswith("/listings/{id}/heartbeat")
    assert heartbeat["action"] == "heartbeat-listing" and "24 hours" in heartbeat["limit"]
    assert "cursor (preferred)" in params["endpoints"]["browse"]["queryParams"]

    assert "409 duplicate_listing" in params["duplicateDetection"]["rule"]
    assert "never modifies" in params["duplicateDetection"]["rule"]
    assert params["freshness"]["stale"].startswith(f"true when last_activity_at is older than {activity.STALE_AFTER_DAYS:g}")
    assert "last_activity_at descending" in params["freshness"]["defaultSort"]
    assert "never calls a listing's endpoint" in params["freshness"]["stale"]
    assert params["taskCategories"] == list(TASK_CATEGORIES)


def test_the_manifest_documents_the_error_contract() -> None:
    errors = _params()["errors"]
    assert {c["error_code"] for c in errors["codes"]} == set(ERROR_CODES)
    for code in errors["codes"]:
        assert {"error_code", "http_status", "retryable", "description"} == set(code)
    assert "next_actions" in errors["shape"]["properties"] and "error_code" in errors["shape"]["properties"]
    assert "header:X-Wallet-Auth" in errors["nextActionsConvention"]
    for stable in ("duplicate_listing", "rate_limited", "invalid_signature", "stale_signature", "not_found"):
        assert stable in ERROR_CODES


def test_the_manifest_schemas_include_the_new_fields() -> None:
    params = _params()
    response_fields = params["outputSchema"]["properties"]
    assert {"last_seen_at", "last_activity_at", "stale", "payment_options"} <= set(response_fields)
    assert "next_cursor" in params["listSchema"]["properties"]
    assert {"next_heartbeat_allowed_at", "last_seen_at"} <= set(params["heartbeatSchema"]["properties"])
    assert "payment_options" in params["inputSchema"]["properties"]


def test_the_agent_card_version_matches_the_service_version() -> None:
    assert client.get("/.well-known/agent-card.json").json()["version"] == SERVICE_VERSION
    assert client.get("/openapi.json").json()["info"]["version"] == SERVICE_VERSION


def test_the_mcp_extension_is_still_listed() -> None:
    card = client.get("/.well-known/agent-card.json").json()
    assert any(e["uri"].endswith("mcp:v1") for e in card["capabilities"]["extensions"])


# ---- OpenAPI ------------------------------------------------------------------------------------


def test_openapi_describes_the_new_routes_parameters_and_fields() -> None:
    spec = client.get("/openapi.json").json()
    assert "/listings/{listing_id}/heartbeat" in spec["paths"]
    browse = spec["paths"]["/listings"]["get"]
    assert "cursor" in {p["name"] for p in browse["parameters"]}
    schemas = spec["components"]["schemas"]
    assert {"last_seen_at", "last_activity_at", "stale", "payment_options"} <= set(schemas["ListingResponse"]["properties"])
    assert "next_cursor" in schemas["ListingsPage"]["properties"]
    assert {"error_code", "message", "detail", "next_actions"} <= set(schemas["ErrorResponse"]["properties"])
    assert "signingSpec" in spec["info"]["description"]


def test_every_write_route_documents_its_error_responses() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        ("/listings", "post"): {"409", "422", "429"},
        ("/listings/{listing_id}", "patch"): {"401", "403", "404", "409", "422", "429"},
        ("/listings/{listing_id}", "delete"): {"401", "403", "404", "429"},
        ("/listings/{listing_id}/heartbeat", "post"): {"401", "403", "404", "409", "429"},
    }
    for (path, method), statuses in expected.items():
        assert statuses <= set(paths[path][method]["responses"]), (path, method)


# ---- llms.txt -----------------------------------------------------------------------------------


def test_llms_txt_is_plain_text_and_covers_the_essentials() -> None:
    response = client.get("/llms.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    for code in ERROR_CODES:
        assert f"- {code} (" in text, code
    for needle in (
        "/listings/{id}/heartbeat",
        "duplicate_listing",
        "existing_listing_id",
        "cursor",
        "stale",
        "payment_options",
        "payment_wallet is deprecated",
        "signingSpec",
        "/.well-known/agent-card.json",
        "/openapi.json",
        "search_listings",
        "X-Wallet-Auth",
    ):
        assert needle in text, needle
    for category in TASK_CATEGORIES:
        assert category in text


def test_llms_txt_uses_the_configured_base_url() -> None:
    from app.api.routes.discovery import SERVICE_BASE_URL

    assert f"{SERVICE_BASE_URL}/listings" in client.get("/llms.txt").text
