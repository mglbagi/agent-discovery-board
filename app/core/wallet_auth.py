"""Wallet-signature authentication for editing or deactivating a listing.

No accounts, no passwords: the submitter proves control of `submitted_by`'s
private key by signing a short, domain-separated message with EIP-191
personal_sign — the same signing scheme MetaMask and most wallets use for
"Sign-In with..." flows, and the same general idea as the verification
service's attestation design (a fixed domain-separator line, then the
specific action and its parameters, so a signature for one thing can never
be replayed as authorization for another).

What gets signed
-----------------
    Agent Discovery Board
    action: update-listing
    listing_id: <id>
    timestamp: <unix seconds>
    nonce: <client-chosen random string>
    body_sha256: <hex sha256 of the canonical JSON PATCH body>

`delete-listing` omits the `body_sha256` line (there is no body). The server
computes `body_sha256` itself, from the request body it actually received
(app/core/canonical.py) — it never trusts a claim about what was signed. So
tampering with the body after it was signed doesn't produce "signature
invalid" as a separate check; it produces a DIFFERENT expected message, which
means the given signature recovers a WRONG address, which fails the identity
check below. There is exactly one check: does this signature, over the
message the server itself reconstructs, recover `submitted_by`?

Sent as a single request header, `X-Wallet-Auth`: base64 of
`{"signature": "0x...", "timestamp": <int>, "nonce": "<str>"}`.

Replay protection
------------------
  * `timestamp` must be within SIGNATURE_MAX_AGE_SECONDS of the server's
    clock, in either direction (bounds how long a captured header stays
    usable, and tolerates ordinary clock skew);
  * (submitted_by, nonce) may not repeat within that same window — tracked
    in memory, self-expiring — so a captured header can't be replayed even
    once, not even for the exact same edit.

Errors
------
  * 401: the header is missing, malformed, expired, or its nonce was
    already used — something is wrong with the authentication attempt
    itself, before we even get to whose signature it is.
  * 403: the header parses fine and recovers a real address, but that
    address is not `submitted_by` — authenticated as the wrong party.
"""

import base64
import binascii
import hashlib
import json
import os
import threading
import time
from typing import Any

from fastapi import HTTPException, Request

from app.core.canonical import canonical_json

SIGNATURE_MAX_AGE_SECONDS = float(os.getenv("SIGNATURE_MAX_AGE_SECONDS", "300"))
_MAX_TRACKED_NONCES = 50_000
_HEADER_NAME = "x-wallet-auth"

_nonce_lock = threading.Lock()
_seen_nonces: dict[tuple[str, str], float] = {}  # (address_lower, nonce) -> monotonic insert time


def _prune_expired_nonces(now: float) -> None:
    # Called with the lock held. A used nonce only needs remembering for as long as
    # its timestamp could still fall inside the freshness window.
    horizon = SIGNATURE_MAX_AGE_SECONDS * 2
    if len(_seen_nonces) <= _MAX_TRACKED_NONCES:
        for key, seen_at in list(_seen_nonces.items()):
            if now - seen_at > horizon:
                del _seen_nonces[key]
    else:
        for key, seen_at in list(_seen_nonces.items()):
            if now - seen_at > horizon:
                del _seen_nonces[key]


def _claim_nonce(address_lower: str, nonce: str) -> bool:
    """Returns True if this (address, nonce) pair is being used for the first time."""
    now = time.monotonic()
    with _nonce_lock:
        _prune_expired_nonces(now)
        key = (address_lower, nonce)
        if key in _seen_nonces:
            return False
        _seen_nonces[key] = now
        return True


def _build_message(*, action: str, listing_id: str, timestamp: int, nonce: str, body: dict[str, Any] | None) -> str:
    lines = [
        "Agent Discovery Board",
        f"action: {action}",
        f"listing_id: {listing_id}",
        f"timestamp: {timestamp}",
        f"nonce: {nonce}",
    ]
    if body is not None:
        digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        lines.append(f"body_sha256: {digest}")
    return "\n".join(lines)


def _parse_header(request: Request) -> tuple[str, int, str]:
    raw = request.headers.get(_HEADER_NAME)
    if not raw:
        raise HTTPException(status_code=401, detail=f"Missing {_HEADER_NAME.upper()} header.")
    try:
        decoded = base64.b64decode(raw, validate=True)
        payload = json.loads(decoded)
        signature = payload["signature"]
        timestamp = int(payload["timestamp"])
        nonce = payload["nonce"]
        if not isinstance(signature, str) or not isinstance(nonce, str):
            raise ValueError("wrong field types")
        if not (1 <= len(nonce) <= 128):
            raise ValueError("nonce length out of range")
    except (binascii.Error, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=401,
            detail=f"{_HEADER_NAME.upper()} header is malformed. Expected base64 of "
            '{"signature": "0x...", "timestamp": <int>, "nonce": "<string>"}.',
        ) from exc
    return signature, timestamp, nonce


def verify_wallet_auth(
    request: Request, *, action: str, listing_id: str, submitted_by: str, body: dict[str, Any] | None
) -> None:
    """Raises HTTPException(401) or HTTPException(403) on any failure; returns
    None (does nothing) on success."""
    signature, timestamp, nonce = _parse_header(request)

    now = int(time.time())
    if abs(now - timestamp) > SIGNATURE_MAX_AGE_SECONDS:
        raise HTTPException(
            status_code=401,
            detail=f"Signature timestamp is stale or from the future (must be within "
            f"{SIGNATURE_MAX_AGE_SECONDS:.0f}s of the server's clock).",
        )

    submitted_by_lower = submitted_by.lower()
    if not _claim_nonce(submitted_by_lower, nonce):
        raise HTTPException(status_code=401, detail="This (wallet, nonce) pair has already been used.")

    message = _build_message(action=action, listing_id=listing_id, timestamp=timestamp, nonce=nonce, body=body)

    from eth_account import Account
    from eth_account.messages import encode_defunct

    try:
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception as exc:  # noqa: BLE001 — any malformed/invalid signature bytes
        raise HTTPException(status_code=401, detail="Signature is not a valid Ethereum signature.") from exc

    if recovered.lower() != submitted_by_lower:
        raise HTTPException(
            status_code=403,
            detail="Signature is valid but was not made by this listing's submitted_by address.",
        )
