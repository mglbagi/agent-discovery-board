"""Unit tests for the trust-score badge client's caching/enable-disable logic.

These monkeypatch score_client._fetch or the underlying x402 HTTP client rather than
talking to a real network - see test_score_client_x402_integration.py for tests that
exercise the real x402 payment-client code end to end against a mocked score service.
"""

import time
from datetime import datetime

import pytest

from app.core import score_client

FAKE_KEY = "0x" + "11" * 32


@pytest.fixture(autouse=True)
def _isolated_score_client_state(monkeypatch):
    monkeypatch.setattr(score_client, "_PAYER_KEY", None)
    monkeypatch.setattr(score_client, "_signing_client", None)
    score_client.reset_cache()
    yield
    score_client.reset_cache()


def test_disabled_by_default() -> None:
    assert score_client.badge_lookups_enabled() is False


def test_enabled_once_a_key_is_set(monkeypatch) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", FAKE_KEY)
    assert score_client.badge_lookups_enabled() is True


async def test_disabled_returns_none_without_network_call(monkeypatch) -> None:
    async def _boom(agent_id):
        raise AssertionError("must not attempt a network call when badges are disabled")

    monkeypatch.setattr(score_client, "_fetch", _boom)
    assert await score_client.get_badge("some-agent") is None


async def test_missing_agent_id_returns_none_without_network_call(monkeypatch) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", FAKE_KEY)

    async def _boom(agent_id):
        raise AssertionError("must not attempt a network call for a missing agent_id")

    monkeypatch.setattr(score_client, "_fetch", _boom)
    assert await score_client.get_badge(None) is None
    assert await score_client.get_badge("") is None


async def test_successful_fetch_is_cached(monkeypatch) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", FAKE_KEY)
    calls: list[str] = []

    async def _fake_fetch(agent_id):
        calls.append(agent_id)
        return {"trust_score": 0.9}

    monkeypatch.setattr(score_client, "_fetch", _fake_fetch)

    first = await score_client.get_badge("agent-1")
    second = await score_client.get_badge("agent-1")
    assert first == {"trust_score": 0.9}
    assert second == {"trust_score": 0.9}
    assert calls == ["agent-1"]  # second call was served from cache, not refetched


async def test_cache_is_keyed_per_agent(monkeypatch) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", FAKE_KEY)
    calls: list[str] = []

    async def _fake_fetch(agent_id):
        calls.append(agent_id)
        return {"agent": agent_id}

    monkeypatch.setattr(score_client, "_fetch", _fake_fetch)
    await score_client.get_badge("agent-1")
    await score_client.get_badge("agent-2")
    assert calls == ["agent-1", "agent-2"]


async def test_cache_expires_after_ttl(monkeypatch) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", FAKE_KEY)
    monkeypatch.setattr(score_client, "BADGE_CACHE_TTL_SECONDS", 0.1)
    calls: list[str] = []

    async def _fake_fetch(agent_id):
        calls.append(agent_id)
        return {"n": len(calls)}

    monkeypatch.setattr(score_client, "_fetch", _fake_fetch)
    await score_client.get_badge("agent-1")
    time.sleep(0.2)
    await score_client.get_badge("agent-1")
    assert calls == ["agent-1", "agent-1"]


async def test_failed_lookup_is_also_cached_to_avoid_hammering_a_down_service(monkeypatch) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", FAKE_KEY)
    calls: list[str] = []

    async def _fake_fetch(agent_id):
        calls.append(agent_id)
        return None

    monkeypatch.setattr(score_client, "_fetch", _fake_fetch)
    assert await score_client.get_badge("agent-1") is None
    assert await score_client.get_badge("agent-1") is None
    assert calls == ["agent-1"]  # not retried within the TTL


def test_to_badge_maps_verification_service_fields() -> None:
    badge = score_client._to_badge(
        {
            "trust_score": 0.83,
            "confidence_interval_95": [0.81, 0.85],
            "sample_size": 1847,
            "identity_verified": False,
            "reason": None,
        }
    )
    assert badge["trust_score"] == 0.83
    assert badge["confidence_interval_95"] == [0.81, 0.85]
    assert badge["sample_size"] == 1847
    assert badge["identity_verified"] is False
    assert badge["source"] == f"{score_client.VERIFICATION_SERVICE_URL}/score/{{agent_id}}"
    datetime.fromisoformat(badge["fetched_at"])  # does not raise


def test_to_badge_defaults_sample_size_to_zero_when_missing() -> None:
    badge = score_client._to_badge({})
    assert badge["sample_size"] == 0
    assert badge["trust_score"] is None


async def test_fetch_returns_none_on_non_200(monkeypatch) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", FAKE_KEY)
    monkeypatch.setattr(score_client, "_build_signing_client", lambda: object())

    class _FakeResponse:
        status_code = 500

        def json(self):
            raise AssertionError("must not parse the body of a non-200 response")

    class _FakeHTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url):
            return _FakeResponse()

    monkeypatch.setattr("x402.http.clients.httpx.x402HttpxClient", lambda client, timeout=None: _FakeHTTP())
    assert await score_client._fetch("agent-1") is None


async def test_fetch_returns_none_on_malformed_json(monkeypatch) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", FAKE_KEY)
    monkeypatch.setattr(score_client, "_build_signing_client", lambda: object())

    class _FakeResponse:
        status_code = 200

        def json(self):
            raise ValueError("not json")

    class _FakeHTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url):
            return _FakeResponse()

    monkeypatch.setattr("x402.http.clients.httpx.x402HttpxClient", lambda client, timeout=None: _FakeHTTP())
    assert await score_client._fetch("agent-1") is None


async def test_fetch_returns_none_when_signing_client_cannot_be_built(monkeypatch) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", FAKE_KEY)

    def _boom():
        raise RuntimeError("not a valid private key")

    monkeypatch.setattr(score_client, "_build_signing_client", _boom)
    assert await score_client._fetch("agent-1") is None


async def test_fetch_returns_none_on_transport_exception(monkeypatch) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", FAKE_KEY)
    monkeypatch.setattr(score_client, "_build_signing_client", lambda: object())

    class _FakeHTTP:
        async def __aenter__(self):
            raise ConnectionError("connection refused")

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr("x402.http.clients.httpx.x402HttpxClient", lambda client, timeout=None: _FakeHTTP())
    assert await score_client._fetch("agent-1") is None


async def test_fetch_succeeds_and_hits_the_right_url(monkeypatch) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", FAKE_KEY)
    monkeypatch.setattr(score_client, "_build_signing_client", lambda: object())
    requested_urls: list[str] = []

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"trust_score": 0.77, "sample_size": 12}

    class _FakeHTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url):
            requested_urls.append(url)
            return _FakeResponse()

    monkeypatch.setattr("x402.http.clients.httpx.x402HttpxClient", lambda client, timeout=None: _FakeHTTP())
    badge = await score_client._fetch("agent-1")
    assert badge["trust_score"] == 0.77
    assert requested_urls == [f"{score_client.VERIFICATION_SERVICE_URL}/score/agent-1"]


def test_reset_cache_clears_everything() -> None:
    score_client._cache["x"] = (time.monotonic(), {"trust_score": 1})
    score_client.reset_cache()
    assert score_client._cache == {}


def test_build_signing_client_reuses_the_cached_instance(monkeypatch) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", FAKE_KEY)
    first = score_client._build_signing_client()
    second = score_client._build_signing_client()
    assert first is second


def test_build_signing_client_raises_for_an_invalid_key(monkeypatch) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", "not-a-valid-private-key")
    with pytest.raises(Exception):
        score_client._build_signing_client()
