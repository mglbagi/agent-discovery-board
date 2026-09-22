"""End-to-end test of the REAL x402 payment client code in app/core/score_client.py
(x402Client + EthAccountSigner + x402HttpxClient - the exact classes _build_signing_client
and _fetch use), run against a mocked score service: a local HTTP server that speaks the
real x402 protocol (a genuine 402 PaymentRequired challenge with a real EIP-712 domain,
then 200 once a validly-shaped, validly-signed payment header is attached). Only the
network endpoint is fake - the payment-client code path itself is real, unmodified, and
never monkeypatched in this file (contrast test_score_client.py, which does monkeypatch
it, for pure caching/error-handling tests).

No blockchain, facilitator, or real funds are involved: this mock service accepts
whatever validly-formed payment payload the client produces without settling it
on-chain, exactly like the real verification service does for a payment it doesn't
recognize as ever needing to check the client's balance client-side (all balance/
authenticity checks happen at the real facilitator, which only the SERVER side would
call - simulated here on the "score service" side, out of scope for a client-side test).
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from eth_account import Account
from x402.http.utils import decode_payment_signature_header, encode_payment_required_header
from x402.schemas import PaymentRequired, PaymentRequirements

from app.core import score_client

TEST_PAYER_KEY = "0x" + "22" * 32
# A real, recognized default USDC asset for Base Sepolia (app/core/score_client.py's
# spend controls only allow known default assets unless explicitly configured
# otherwise - an arbitrary/unknown token address is rejected client-side before any
# request is even sent, which was confirmed while building this test).
BASE_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"

AGENT_ID = "https://example.com/agents/summarizer"
SCORE_BODY = {
    "agent_id": AGENT_ID,
    "trust_score": 0.91,
    "sample_size": 42,
    "confidence_interval_95": [0.83, 0.97],
    "identity_verified": False,
    "reason": None,
    "computed_at": "2026-01-01T00:00:00+00:00",
}


def _payment_required_header(pay_to: str) -> str:
    requirements = PaymentRequirements(
        scheme="exact",
        network="eip155:84532",
        asset=BASE_SEPOLIA_USDC,
        amount="10000",
        pay_to=pay_to,
        max_timeout_seconds=3600,
        extra={"name": "USDC", "version": "2"},
    )
    return encode_payment_required_header(PaymentRequired(accepts=[requirements]))


class _MockScoreService(BaseHTTPRequestHandler):
    pay_to = ""  # set per-test-run by the fixture
    seen_payment_payloads: list = []
    request_count = 0

    def log_message(self, *args) -> None:  # silence BaseHTTPRequestHandler's default logging
        pass

    def do_GET(self) -> None:
        type(self).request_count += 1
        signature_header = self.headers.get("Payment-Signature")

        if not signature_header:
            body = b"{}"
            self.send_response(402)
            self.send_header("PAYMENT-REQUIRED", _payment_required_header(self.pay_to))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        type(self).seen_payment_payloads.append(decode_payment_signature_header(signature_header))
        body = json.dumps(SCORE_BODY).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def mock_score_service():
    _MockScoreService.pay_to = Account.create().address
    _MockScoreService.seen_payment_payloads = []
    _MockScoreService.request_count = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockScoreService)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _isolated_score_client_state(monkeypatch):
    monkeypatch.setattr(score_client, "_signing_client", None)
    score_client.reset_cache()
    yield
    score_client.reset_cache()


async def test_real_x402_client_pays_the_402_challenge_and_returns_the_score(monkeypatch, mock_score_service) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", TEST_PAYER_KEY)
    monkeypatch.setattr(score_client, "VERIFICATION_SERVICE_URL", mock_score_service)

    badge = await score_client.get_badge(AGENT_ID)

    assert badge is not None
    assert badge["trust_score"] == 0.91
    assert badge["sample_size"] == 42
    assert _MockScoreService.request_count == 2  # 1: unpaid -> 402, 2: paid retry -> 200
    assert len(_MockScoreService.seen_payment_payloads) == 1

    payload = _MockScoreService.seen_payment_payloads[0]
    payer_address = payload.payload["authorization"]["from"]
    assert payer_address.lower() == Account.from_key(TEST_PAYER_KEY).address.lower()
    assert payload.accepted.pay_to.lower() == _MockScoreService.pay_to.lower()
    assert payload.accepted.asset.lower() == BASE_SEPOLIA_USDC.lower()
    assert payload.accepted.amount == "10000"


async def test_result_is_cached_so_a_second_lookup_does_not_pay_again(monkeypatch, mock_score_service) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", TEST_PAYER_KEY)
    monkeypatch.setattr(score_client, "VERIFICATION_SERVICE_URL", mock_score_service)

    first = await score_client.get_badge(AGENT_ID)
    second = await score_client.get_badge(AGENT_ID)

    assert first == second
    assert len(_MockScoreService.seen_payment_payloads) == 1  # the cached call made no HTTP request at all
    assert _MockScoreService.request_count == 2


async def test_disabled_board_never_contacts_the_mock_service(monkeypatch, mock_score_service) -> None:
    monkeypatch.setattr(score_client, "_PAYER_KEY", None)
    monkeypatch.setattr(score_client, "VERIFICATION_SERVICE_URL", mock_score_service)

    badge = await score_client.get_badge(AGENT_ID)

    assert badge is None
    assert _MockScoreService.request_count == 0
