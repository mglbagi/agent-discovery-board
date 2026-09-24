"""The exact signing spec, published in the discovery manifest so an agent can sign a
request without reading any prose documentation.

Everything here is derived from the code that actually verifies signatures
(app/core/wallet_auth.py): the message is built with wallet_auth._build_message, the
body hash with canonical_json, the window from SIGNATURE_MAX_AGE_SECONDS. The worked
example is computed, not typed in, so it cannot drift - tests/test_signing_spec.py
recovers the example signature back to the example address.

The example key below is a PUBLICLY KNOWN throwaway test key (it is the first default
account of the popular Hardhat/Anvil dev chains). It exists only so an implementer can
reproduce the example signature byte for byte. It controls nothing; never use it, or any
key that appears in documentation, for a real listing.
"""

import base64
import hashlib
import json
from functools import lru_cache
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct

from app.core.canonical import canonical_json
from app.core.reserved import HARDHAT_ACCOUNT_0
from app.core.wallet_auth import (
    MESSAGE_FIRST_LINE,
    SIGNATURE_MAX_AGE_SECONDS,
    SIGNED_ACTIONS,
    _build_message,
)

EXAMPLE_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
EXAMPLE_ADDRESS = HARDHAT_ACCOUNT_0
EXAMPLE_LISTING_ID = "11111111-2222-4333-8444-555555555555"
EXAMPLE_TIMESTAMP = 1790000000
EXAMPLE_NONCE = "0123456789abcdef0123456789abcdef"
EXAMPLE_PATCH_BODY = {"pricing_amount": "$0.03"}


def _sign(message: str) -> str:
    signed = Account.from_key(EXAMPLE_PRIVATE_KEY).sign_message(encode_defunct(text=message))
    raw = signed.signature.hex()
    return raw if raw.startswith("0x") else "0x" + raw


def _header(signature: str) -> tuple[dict[str, Any], str]:
    payload = {"signature": signature, "timestamp": EXAMPLE_TIMESTAMP, "nonce": EXAMPLE_NONCE}
    return payload, base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


@lru_cache(maxsize=1)
def worked_example() -> dict[str, Any]:
    patch_message = _build_message(
        action="update-listing",
        listing_id=EXAMPLE_LISTING_ID,
        timestamp=EXAMPLE_TIMESTAMP,
        nonce=EXAMPLE_NONCE,
        body=EXAMPLE_PATCH_BODY,
    )
    patch_signature = _sign(patch_message)
    patch_header_json, patch_header = _header(patch_signature)

    heartbeat_message = _build_message(
        action="heartbeat-listing",
        listing_id=EXAMPLE_LISTING_ID,
        timestamp=EXAMPLE_TIMESTAMP,
        nonce=EXAMPLE_NONCE,
        body=None,
    )
    heartbeat_signature = _sign(heartbeat_message)
    heartbeat_header_json, heartbeat_header = _header(heartbeat_signature)

    return {
        "note": "EXAMPLE VALUES. The private key is public and controls nothing. In a real request use your own "
        "wallet, the real listing id, a timestamp within the allowed window of the server clock, and a fresh nonce.",
        "example_private_key": EXAMPLE_PRIVATE_KEY,
        "example_address": EXAMPLE_ADDRESS,
        "listing_id": EXAMPLE_LISTING_ID,
        "timestamp": EXAMPLE_TIMESTAMP,
        "nonce": EXAMPLE_NONCE,
        "update_listing": {
            "request": {"method": "PATCH", "path": f"/listings/{EXAMPLE_LISTING_ID}", "json_body": EXAMPLE_PATCH_BODY},
            "canonical_body_json": canonical_json(EXAMPLE_PATCH_BODY),
            "body_sha256": hashlib.sha256(canonical_json(EXAMPLE_PATCH_BODY).encode("utf-8")).hexdigest(),
            "message": patch_message,
            "signature": patch_signature,
            "x_wallet_auth_json": patch_header_json,
            "x_wallet_auth_header": patch_header,
        },
        "heartbeat_listing": {
            "request": {"method": "POST", "path": f"/listings/{EXAMPLE_LISTING_ID}/heartbeat", "json_body": None},
            "message": heartbeat_message,
            "signature": heartbeat_signature,
            "x_wallet_auth_json": heartbeat_header_json,
            "x_wallet_auth_header": heartbeat_header,
        },
    }


def signing_spec() -> dict[str, Any]:
    return {
        "scheme": "EIP-191 personal_sign (the 'Ethereum Signed Message' prefix; what eth_account "
        "encode_defunct / MetaMask personal_sign / ethers signMessage produce) over the plain-text message below. "
        "Not a transaction and not EIP-712 typed data.",
        "signer": "The wallet in the listing's submitted_by (compared case-insensitively). Only an ordinary "
        "key-based (EOA) wallet works: smart-contract wallet signatures (ERC-1271) cannot be verified.",
        "header": {
            "name": "X-Wallet-Auth",
            "encoding": "base64 (standard alphabet, padded) of the UTF-8 JSON object "
            '{"signature": "0x...", "timestamp": <integer unix seconds>, "nonce": "<string>"}',
            "signature": "0x-prefixed 65-byte hex (r || s || v) as returned by personal_sign",
            "nonce": "1-128 characters, chosen by you; a given (wallet, nonce) pair is accepted once within the window",
        },
        "message_template": {
            "join_lines_with": "\n (LF); no trailing newline; no blank lines; no extra whitespace",
            "lines": [
                MESSAGE_FIRST_LINE,
                "action: <action>",
                "listing_id: <listing id from the request path>",
                "timestamp: <the same integer unix seconds as in the header>",
                "nonce: <the same nonce as in the header>",
                "body_sha256: <lowercase hex sha256 of the canonical JSON body> (only when the action signs a body)",
            ],
        },
        "actions": {
            name: {
                "http": f"{meta['method']} {meta['path']}",
                "signs_body_hash": meta["signs_body_hash"],
                "body_sha256_line": "included" if meta["signs_body_hash"] else "omitted (no body is signed)",
            }
            for name, meta in SIGNED_ACTIONS.items()
        },
        "body_hash": {
            "input": "the JSON object you send as the request body",
            "canonicalization": "json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False), "
            "encoded as UTF-8; keys sorted at every level, no whitespace, non-ASCII kept literal",
            "digest": "SHA-256, lowercase hex, no 0x prefix",
            "note": "The server hashes the body it actually received, so any change after signing makes the "
            "signature recover a different address (wrong_signer).",
        },
        "time_window": {
            "max_age_seconds": int(SIGNATURE_MAX_AGE_SECONDS),
            "rule": "abs(server_unix_time - timestamp) <= max_age_seconds, in either direction. On failure the "
            "stale_signature error returns server_time so you can correct your clock.",
        },
        "failure_codes": {
            "missing_signature": "401, no X-Wallet-Auth header",
            "malformed_signature": "401, header is not base64 of the documented JSON",
            "stale_signature": "401, timestamp outside the window",
            "replayed_signature": "401, this (wallet, nonce) was already used",
            "invalid_signature": "401, not a valid Ethereum signature",
            "wrong_signer": "403, valid signature but not by submitted_by (also what a tampered body or "
            "listing id looks like)",
        },
        "worked_example": worked_example(),
    }
