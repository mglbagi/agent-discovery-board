"""Shared test helpers: building a valid X-Wallet-Auth header the way a real client
would, and baseline listing payloads for each listing_type."""

import base64
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from eth_account.messages import encode_defunct

from app.core.wallet_auth import _build_message


def _sig_hex(signed) -> str:
    raw = signed.signature.hex()
    return raw if raw.startswith("0x") else "0x" + raw


def wallet_auth_header(
    account, *, action: str, listing_id: str, body: dict | None = None, timestamp: int | None = None, nonce: str | None = None
) -> str:
    """`body` must be the exact same dict that will be sent as the request's JSON
    body (for update-listing) - app/core/wallet_auth.py hashes app/core/canonical.py's
    canonical form of it, so the caller doesn't need to pre-canonicalize anything
    itself, just pass the same dict both places."""
    timestamp = int(time.time()) if timestamp is None else timestamp
    nonce = nonce or uuid.uuid4().hex
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
        # Offerings get a unique URL: an active OFFERING with the same normalized
        # endpoint_url and submitted_by is a duplicate (409), and many tests reuse one
        # owner. Announcements, notices and requests are exempt, so they share a
        # realistic URL, as they would in practice.
        "endpoint_url": (
            f"https://example.com/agents/{uuid.uuid4().hex}" if listing_type == "offering" else "https://example.com/agents/example"
        ),
        "payment_wallet": submitted_by,
        "submitted_by": submitted_by,
    }
    if listing_type not in ("announcement", "notice"):
        base["pricing_model"] = "per_call"
        base["pricing_amount"] = "$0.05"
    base.update(overrides)
    return base


def set_listing_times(
    listing_id: str, *, created_days_ago=None, updated_days_ago=None, last_seen_hours_ago="unchanged"
) -> None:
    """Rewrite a listing's timestamps directly in the database, to build old/stale
    listings without waiting. `last_seen_hours_ago=None` clears last_seen_at."""
    from app.core import db

    now = datetime.now(timezone.utc)
    sets, params = [], {"id": listing_id}
    if created_days_ago is not None:
        sets.append("created_at = %(c)s")
        params["c"] = now - timedelta(days=created_days_ago)
    if updated_days_ago is not None:
        sets.append("updated_at = %(u)s")
        params["u"] = now - timedelta(days=updated_days_ago)
    if last_seen_hours_ago != "unchanged":
        sets.append("last_seen_at = %(s)s")
        params["s"] = None if last_seen_hours_ago is None else now - timedelta(hours=last_seen_hours_ago)
    with db._connection() as conn:
        conn.execute(f"UPDATE listings SET {', '.join(sets)} WHERE id = %(id)s", params)


def db_row(listing_id: str) -> dict:
    from app.core import db

    return db.get_listing(listing_id)


def assert_error(response, status: int, code: str) -> dict:
    """Every error, from anywhere, has the same machine-readable shape."""
    body = response.json()
    assert response.status_code == status, body
    assert body["error_code"] == code, body
    assert isinstance(body["message"], str) and body["message"]
    assert "detail" in body
    assert isinstance(body["next_actions"], list) and body["next_actions"]
    for act in body["next_actions"]:
        assert set(act) == {"method", "path", "required_fields", "description"}, act
        assert isinstance(act["required_fields"], list)
        assert act["description"]
    return body


def assert_error_body(body: dict, code: str) -> dict:
    """The same contract as assert_error, for an already-parsed body (e.g. an MCP result)."""
    assert body["error_code"] == code, body
    assert isinstance(body["message"], str) and body["message"] and "detail" in body
    assert body["next_actions"]
    for act in body["next_actions"]:
        assert set(act) == {"method", "path", "required_fields", "description"}, act
    return body
