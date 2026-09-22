from fastapi.testclient import TestClient

from app.core import score_client
from app.main import app

client = TestClient(app)


def test_agent_card_is_served_at_both_paths() -> None:
    for path in ("/.well-known/agent-card.json", "/.well-known/agent-card"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()["name"] == "Agent Discovery Board"


def test_agent_card_has_no_x402_payment_extension() -> None:
    # This service charges nothing for its own endpoints - unlike the verification
    # service's agent-card, there must be no a2a-x402 payment extension here.
    card = client.get("/.well-known/agent-card.json").json()
    uris = [ext["uri"] for ext in card["capabilities"]["extensions"]]
    assert not any("x402" in uri.lower() for uri in uris)


def test_agent_card_lists_known_listing_types_and_task_categories() -> None:
    card = client.get("/.well-known/agent-card.json").json()
    extension = card["capabilities"]["extensions"][0]
    assert set(extension["params"]["knownListingTypes"]["values"]) == {
        "offering",
        "request",
        "announcement",
        "notice",
    }
    assert "data extraction" in extension["params"]["taskCategories"]


def test_agent_card_reflects_badge_configuration_state(monkeypatch) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", None)
    card = client.get("/.well-known/agent-card.json").json()
    extension = card["capabilities"]["extensions"][0]
    assert extension["params"]["trustScoreBadge"]["currentlyConfigured"] is False

    monkeypatch.setattr(score_client, "_PAYER_KEY", "0x" + "11" * 32)
    card = client.get("/.well-known/agent-card.json").json()
    extension = card["capabilities"]["extensions"][0]
    assert extension["params"]["trustScoreBadge"]["currentlyConfigured"] is True


def test_health_check() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
