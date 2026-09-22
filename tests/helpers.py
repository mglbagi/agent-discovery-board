"""Shared test helpers: building a valid X-Wallet-Auth header the way a real client
would, and baseline listing payloads for each listing_type."""

import base64
import json
import time
import uuid

from eth_account.messages import encode_defunct

from app.core.wallet_auth import _build_message


def _sig_hex(signed) -> str:
    raw = signed.signature.hex()
    return raw if raw.startswith("0x") else "0x" + raw


def wallet_auth_header(account, *, action: str, listing_id: str, body: dict | None = None) -> str:
    """`body` must be the exact same dict that will be sent as the request's JSON
    body (for update-listing) - app/core/wallet_auth.py hashes app/core/canonical.py's
    canonical form of it, so the caller doesn't need to pre-canonicalize anything
    itself, just pass the same dict both places."""
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    message = _build_message(action=action, listing_id=listing_id, timestamp=timestamp, nonce=nonce, body=body)
    signed = account.sign_message(encode_defunct(text=message))
    payload = {"signature": _sig_hex(signed), "timestamp": timestamp, "nonce": nonce}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def listing_payload(listing_type: str, submitted_by: str, **overrides) -> dict:
    base = {
        "name": f"Example {listing_type.title()}",
        "description": f"An example {listing_type} listing used in tests.",
        "listing_type": listing_type,
        "task_categories": ["other"],
        "endpoint_url": "https://example.com/agents/example",
        "payment_wallet": submitted_by,
        "submitted_by": submitted_by,
    }
    if listing_type not in ("announcement", "notice"):
        base["pricing_model"] = "per_call"
        base["pricing_amount"] = "$0.05"
    base.update(overrides)
    return base
