"""Client for the verification service's paid GET /score/{agent_id}, used to attach a
live trust-score badge to a listing.

Fails soft, always. Per the spec this badge integration exists under: "if no score
is available, the listing just shows without a badge — this must not be required or
block listing creation." Every failure mode here — no payer key configured, the
verification service being unreachable or slow, a malformed response, insufficient
balance, anything — results in `get_badge()` returning None, logged but never
raised. Nothing in the listings routes treats a badge failure as a request failure.

Shipped state: UNCONFIGURED, ON PURPOSE. GET /score/{agent_id} costs $0.01 USDC per
call with no free tier over REST, so a badge is a real recurring cost, not a free
extra. Until BOARD_PAYER_PRIVATE_KEY is set, badge_lookups_enabled() is False and
get_badge() returns None immediately — no network call, no attempted spend. This is
the default this service deploys with, not a placeholder that needs finishing before
going live; the rest of the board works fully with every badge simply absent.

To enable: fund a small, DEDICATED wallet (never one holding anything else) with a
little USDC on Base mainnet and set BOARD_PAYER_PRIVATE_KEY. Real x402 payment
client code below — the same pattern as the verification service's own
_pay_mainnet_real.py (x402Client + EthAccountSigner + x402HttpxClient) — so turning
it on is only a matter of setting that one env var.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("app.score_client")

VERIFICATION_SERVICE_URL = os.getenv(
    "VERIFICATION_SERVICE_URL", "https://fastapi-service-5ag4.onrender.com"
).rstrip("/")
BADGE_CACHE_TTL_SECONDS = float(os.getenv("BADGE_CACHE_TTL_SECONDS", "300"))
BADGE_FETCH_TIMEOUT_SECONDS = float(os.getenv("BADGE_FETCH_TIMEOUT_SECONDS", "15"))

_PAYER_KEY = os.getenv("BOARD_PAYER_PRIVATE_KEY")

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_signing_client = None  # lazily built on first real use


def badge_lookups_enabled() -> bool:
    return bool(_PAYER_KEY)


def _build_signing_client():
    global _signing_client
    if _signing_client is not None:
        return _signing_client
    from eth_account import Account
    from x402 import x402Client
    from x402.mechanisms.evm.exact import register_exact_evm_client
    from x402.mechanisms.evm.signers import EthAccountSigner

    account = Account.from_key(_PAYER_KEY)
    client = x402Client()
    register_exact_evm_client(client, EthAccountSigner(account))
    logger.info("[badge] x402 signing client ready, paying from %s", account.address)
    _signing_client = client
    return _signing_client


def _to_badge(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "trust_score": data.get("trust_score"),
        "confidence_interval_95": data.get("confidence_interval_95"),
        "sample_size": data.get("sample_size", 0),
        "identity_verified": data.get("identity_verified"),
        "reason": data.get("reason"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": f"{VERIFICATION_SERVICE_URL}/score/{{agent_id}}",
    }


async def _fetch(agent_id: str) -> dict[str, Any] | None:
    from x402.http.clients.httpx import x402HttpxClient

    try:
        client = _build_signing_client()
        async with x402HttpxClient(client, timeout=BADGE_FETCH_TIMEOUT_SECONDS) as http:
            response = await http.get(f"{VERIFICATION_SERVICE_URL}/score/{agent_id}")
        if response.status_code != 200:
            logger.warning("[badge] lookup for %r returned HTTP %s", agent_id, response.status_code)
            return None
        return _to_badge(response.json())
    except Exception:  # noqa: BLE001 — a badge is best-effort; never let this bubble up
        logger.exception("[badge] lookup for %r failed", agent_id)
        return None


async def get_badge(agent_id: str | None) -> dict[str, Any] | None:
    if not agent_id or not badge_lookups_enabled():
        return None

    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(agent_id)
        if cached is not None and now - cached[0] < BADGE_CACHE_TTL_SECONDS:
            return cached[1]

    badge = await _fetch(agent_id)
    with _cache_lock:
        _cache[agent_id] = (now, badge)
    return badge


def reset_cache() -> None:
    """Test helper."""
    with _cache_lock:
        _cache.clear()
