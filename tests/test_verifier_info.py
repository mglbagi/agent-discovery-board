"""verify_output's price, networks, protocol and free path come from the verification
service's own public documents (never hard-coded), cached with a background refresh; if they
cannot be read the last-known values are kept and marked as such, and a listing response never
breaks. No test here touches the real service: the document fetch is injected."""

import asyncio
import copy
import time
import uuid

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app.core import db, verifier_info
from app.core.score_client import VERIFICATION_SERVICE_URL
from app.main import app
from tests.helpers import listing_payload

client = TestClient(app)

VERIFY_URL = f"{VERIFICATION_SERVICE_URL}/verify/schema"
MCP_URL = f"{VERIFICATION_SERVICE_URL}/mcp"
BASE = "eip155:8453"
SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"

X402_DOC = {
    "x402Version": 2,
    "resources": [
        {"url": f"{VERIFICATION_SERVICE_URL}/score/{{agent_id}}", "method": "GET", "price": "$0.01", "currency": "USDC",
         "paymentOptions": [{"network": BASE}]},
        {"url": VERIFY_URL, "method": "POST", "price": "$0.02", "currency": "USDC",
         "paymentOptions": [{"chain": "Base", "network": BASE}, {"chain": "Solana", "network": SOLANA}]},
    ],
}
CARD_DOC = {
    "capabilities": {
        "extensions": [
            {"uri": "https://github.com/google-a2a/a2a-x402/v0.1", "params": {}},
            {
                "uri": "urn:json-schema-verifier:extension:mcp:v1",
                "params": {
                    "transport": "streamable-http",
                    "url": MCP_URL,
                    "tools": [
                        {"toolName": "verify_schema", "access": {
                            "freeTrial": {"callsPerClientPerDay": 3, "maxInputBytes": 32768},
                            "afterFreeTrial": "x402 payment, $0.02 USDC per call"}},
                        {"toolName": "get_verification_record", "access": {
                            "freeTrial": {"callsPerClientPerDay": 9, "maxInputBytes": 1}}},
                    ],
                },
            },
        ]
    }
}


def _docs(x402=None, card=None):
    """A stand-in for verifier_info._fetch_json serving these two documents; either may be
    None to simulate that one being unreachable."""
    served = {verifier_info.X402_URL: x402, verifier_info.AGENT_CARD_URL: card}

    def fetch(url):
        doc = served[url]
        if doc is None:
            raise ConnectionError(f"simulated outage for {url}")
        return copy.deepcopy(doc)

    return fetch


@pytest.fixture(autouse=True)
def _clean():
    verifier_info.reset_for_tests()
    with db._connection() as conn:
        conn.execute("DELETE FROM verifier_info_cache")
    yield
    verifier_info.reset_for_tests()
    with db._connection() as conn:
        conn.execute("DELETE FROM verifier_info_cache")


def _listing_with_template() -> dict:
    owner = Account.create()
    payload = listing_payload(
        "offering", owner.address, name=f"vi-{uuid.uuid4().hex[:8]}", output_schema={"type": "object"}
    )
    response = client.post("/listings", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _verify_action(listing: dict) -> dict:
    return next(a for a in listing["next_actions"] if a["action"] == "verify_output")


# ---- parsing the real document shapes ----------------------------------------------------------


def test_parse_payment_picks_the_verify_schema_resource_not_the_score_one() -> None:
    parsed = verifier_info.parse_payment(X402_DOC)
    assert parsed == {
        "endpoint": VERIFY_URL, "method": "POST", "price": "$0.02", "currency": "USDC",
        "networks": [BASE, SOLANA], "protocol": "x402 v2",
    }


def test_parse_free_path_reads_the_verify_schema_tool_not_the_other_one() -> None:
    parsed = verifier_info.parse_free_path(CARD_DOC)
    assert parsed["tool"] == "verify_schema" and parsed["url"] == MCP_URL
    assert parsed["calls_per_client_per_day"] == 3 and parsed["max_input_bytes"] == 32768


@pytest.mark.parametrize("doc", [{}, {"resources": []}, {"resources": [{"url": "https://x/other", "price": "$1"}]}])
def test_parse_payment_rejects_a_document_without_the_resource(doc) -> None:
    with pytest.raises(ValueError):
        verifier_info.parse_payment(doc)


def test_parse_free_path_rejects_a_card_without_the_allowance() -> None:
    with pytest.raises(ValueError):
        verifier_info.parse_free_path({"capabilities": {"extensions": [{"uri": "x:mcp:v1", "params": {"url": MCP_URL}}]}})


# ---- what a listing shows -----------------------------------------------------------------------


def test_verify_output_shows_price_networks_protocol_and_the_free_path(monkeypatch) -> None:
    monkeypatch.setattr(verifier_info, "_fetch_json", _docs(X402_DOC, CARD_DOC))
    assert verifier_info.refresh() is True
    action = _verify_action(_listing_with_template())
    assert action["url"] == VERIFY_URL and action["method"] == "POST"
    assert action["price"] == "$0.02 USDC per check"
    assert action["networks"] == [BASE, SOLANA]
    assert action["protocol"] == "x402 v2"
    assert action["free_path"] == {
        "transport": "streamable-http", "url": MCP_URL, "tool": "verify_schema",
        "calls_per_client_per_day": 3, "max_input_bytes": 32768,
        "after_free_trial": "x402 payment, $0.02 USDC per call",
    }
    assert action["info"]["status"] == "live" and action["info"]["fetched_at"]
    assert set(action["info"]["sources"]) == {verifier_info.X402_URL, verifier_info.AGENT_CARD_URL}
    for fact in ("$0.02", "x402 v2", BASE, "verify_schema", MCP_URL, "3 free calls per client per day"):
        assert fact in action["description"], fact


def test_the_values_follow_the_verifiers_documents_nothing_is_hardcoded(monkeypatch) -> None:
    changed_x402, changed_card = copy.deepcopy(X402_DOC), copy.deepcopy(CARD_DOC)
    changed_x402["resources"][1]["price"] = "$0.05"
    changed_x402["resources"][1]["paymentOptions"] = [{"network": BASE}]
    free = changed_card["capabilities"]["extensions"][1]["params"]["tools"][0]["access"]["freeTrial"]
    free["callsPerClientPerDay"] = 10
    monkeypatch.setattr(verifier_info, "_fetch_json", _docs(changed_x402, changed_card))
    verifier_info.refresh()
    action = _verify_action(_listing_with_template())
    assert action["price"] == "$0.05 USDC per check" and action["networks"] == [BASE]
    assert action["free_path"]["calls_per_client_per_day"] == 10
    assert "10 free calls per client per day" in action["description"]


def test_before_anything_has_ever_been_read_the_listing_still_works_and_says_so() -> None:
    listing = _listing_with_template()  # refresh disabled in tests, nothing cached
    action = _verify_action(listing)
    assert action["price"] is None and action["networks"] == [] and action["free_path"] is None
    assert action["protocol"] is None
    assert action["info"]["status"] == "unavailable" and action["info"]["fetched_at"] is None
    assert action["url"] == VERIFY_URL  # the configured endpoint is still given
    assert "not available right now" in action["description"].lower()
    assert action["body"]["expected_schema"] == {"type": "object"}  # the rest of the action is intact


# ---- outages: last-known values kept and marked, responses never break -------------------------------


def test_an_outage_keeps_the_last_known_values_and_marks_them(monkeypatch) -> None:
    monkeypatch.setattr(verifier_info, "_fetch_json", _docs(X402_DOC, CARD_DOC))
    verifier_info.refresh()
    monkeypatch.setattr(verifier_info, "_fetch_json", _docs(None, None))
    assert verifier_info.refresh() is False

    response = client.post(
        "/listings",
        json=listing_payload("offering", Account.create().address, name="vi-outage", output_schema={"type": "object"}),
    )
    assert response.status_code == 201  # the outage never breaks a response
    action = _verify_action(response.json())
    assert action["price"] == "$0.02 USDC per check" and action["networks"] == [BASE, SOLANA]
    assert action["free_path"]["calls_per_client_per_day"] == 3
    assert action["info"]["status"] == "last_known"


def test_recovery_after_an_outage_goes_back_to_live(monkeypatch) -> None:
    monkeypatch.setattr(verifier_info, "_fetch_json", _docs(X402_DOC, CARD_DOC))
    verifier_info.refresh()
    monkeypatch.setattr(verifier_info, "_fetch_json", _docs(None, None))
    verifier_info.refresh()
    assert verifier_info.snapshot()["status"] == "last_known"
    monkeypatch.setattr(verifier_info, "_fetch_json", _docs(X402_DOC, CARD_DOC))
    verifier_info.refresh()
    assert verifier_info.snapshot()["status"] == "live"


def test_one_document_down_is_partial_and_keeps_the_other_live(monkeypatch) -> None:
    monkeypatch.setattr(verifier_info, "_fetch_json", _docs(X402_DOC, None))
    assert verifier_info.refresh() is False
    snap = verifier_info.snapshot()
    assert snap["status"] == "partial"
    assert snap["payment"]["price"] == "$0.02" and snap["free_path"] is None


def test_a_malformed_document_is_treated_like_an_outage(monkeypatch) -> None:
    monkeypatch.setattr(verifier_info, "_fetch_json", _docs(X402_DOC, CARD_DOC))
    verifier_info.refresh()
    monkeypatch.setattr(verifier_info, "_fetch_json", _docs({"resources": []}, {"capabilities": {}}))
    assert verifier_info.refresh() is False
    snap = verifier_info.snapshot()
    assert snap["status"] == "last_known" and snap["payment"]["price"] == "$0.02"


# ---- last-known values survive a restart ---------------------------------------------------------------


def test_last_known_values_survive_a_restart_while_the_verifier_is_down(monkeypatch) -> None:
    monkeypatch.setattr(verifier_info, "_fetch_json", _docs(X402_DOC, CARD_DOC))
    verifier_info.refresh()
    assert db.load_verifier_info()["payment"]["data"]["price"] == "$0.02"

    verifier_info.reset_for_tests()  # a fresh process: memory empty
    verifier_info._loaded_from_db = False
    monkeypatch.setattr(verifier_info, "_fetch_json", _docs(None, None))
    assert verifier_info.refresh() is False  # loads the stored values, then fails to refresh them
    snap = verifier_info.snapshot()
    assert snap["status"] == "last_known"
    assert snap["payment"]["networks"] == [BASE, SOLANA] and snap["free_path"]["calls_per_client_per_day"] == 3


# ---- the background refresh --------------------------------------------------------------------------------


def test_snapshot_refreshes_in_the_background_without_blocking(monkeypatch) -> None:
    monkeypatch.setenv("VERIFIER_INFO_REFRESH", "1")
    calls = []
    real = _docs(X402_DOC, CARD_DOC)

    def slow_fetch(url):
        calls.append(url)
        time.sleep(0.3)
        return real(url)

    monkeypatch.setattr(verifier_info, "_fetch_json", slow_fetch)
    started = time.monotonic()
    first = verifier_info.snapshot()
    second = verifier_info.snapshot()  # a refresh is already running: must not start another
    assert time.monotonic() - started < 0.25  # neither call waited for the fetch
    assert first["status"] == "unavailable" and second["status"] == "unavailable"
    deadline = time.monotonic() + 5
    while verifier_info.snapshot()["status"] != "live" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert verifier_info.snapshot()["status"] == "live"
    assert len(calls) == 2  # one refresh: the x402 document and the agent-card, once each


def test_after_a_failed_refresh_the_retry_waits_for_the_backoff(monkeypatch) -> None:
    monkeypatch.setenv("VERIFIER_INFO_REFRESH", "1")
    calls = []

    def failing(url):
        calls.append(url)
        raise ConnectionError("down")

    monkeypatch.setattr(verifier_info, "_fetch_json", failing)
    verifier_info.snapshot()
    deadline = time.monotonic() + 5
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    n = len(calls)
    for _ in range(5):
        verifier_info.snapshot()  # within the 60s backoff: no new attempt
    time.sleep(0.3)
    assert len(calls) == n


# ---- same data on REST and the MCP tools ------------------------------------------------------------------


def test_rest_and_the_mcp_tool_functions_show_the_same_verify_output(monkeypatch) -> None:
    from app.api.routes.listings import get_listing, search_listings

    monkeypatch.setattr(verifier_info, "_fetch_json", _docs(X402_DOC, CARD_DOC))
    verifier_info.refresh()
    listing = _listing_with_template()
    rest = _verify_action(client.get(f"/listings/{listing['id']}").json())
    via_get = next(a for a in asyncio.run(get_listing(listing["id"])).model_dump(mode="json")["next_actions"]
                   if a["action"] == "verify_output")
    page = asyncio.run(search_listings(q=listing["name"], listing_type="offering"))
    via_search = next(a for a in next(l for l in page.listings if l.id == listing["id"]).model_dump(mode="json")["next_actions"]
                      if a["action"] == "verify_output")
    assert rest == via_get == via_search
    assert rest["price"] == "$0.02 USDC per check" and rest["free_path"]["tool"] == "verify_schema"


def test_the_manifest_documents_the_new_fields() -> None:
    card = client.get("/.well-known/agent-card.json").json()
    text = card["capabilities"]["extensions"][0]["params"]["nextActions"]["description"]
    for needle in ("free_path", "info.status", "last_known", "/.well-known/x402"):
        assert needle in text, needle
    schema = card["capabilities"]["extensions"][0]["params"]["nextActions"]["schema"]
    assert {"protocol", "free_path", "info"} <= set(schema["properties"])
