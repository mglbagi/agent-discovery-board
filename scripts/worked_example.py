"""Worked example: create one listing of every listing_type, fetch them back, edit
and deactivate one with a real wallet signature, and show search/filter working.

Run against a locally running server (see README "Run the server locally"):

    python scripts/worked_example.py

Or against a deployed instance:

    python scripts/worked_example.py --url https://your-service.onrender.com
"""

import argparse
import base64
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_defunct  # noqa: E402

from app.core.wallet_auth import _build_message  # noqa: E402


def _sig_hex(signed) -> str:
    raw = signed.signature.hex()
    return raw if raw.startswith("0x") else "0x" + raw


def _wallet_auth_header(account, *, action: str, listing_id: str, body: dict | None) -> str:
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    message = _build_message(action=action, listing_id=listing_id, timestamp=timestamp, nonce=nonce, body=body)
    signed = account.sign_message(encode_defunct(text=message))
    payload = {"signature": _sig_hex(signed), "timestamp": timestamp, "nonce": nonce}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _print_header(title: str) -> None:
    print(f"\n{'=' * 10} {title} {'=' * 10}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8200", help="base URL of a running instance")
    args = ap.parse_args()

    account = Account.create()
    print(f"Using a fresh throwaway wallet for this run: {account.address}")

    http = httpx.Client(base_url=args.url, timeout=20)

    _print_header("1. Create one listing of each listing_type")
    listings: dict[str, dict] = {}

    listings["offering"] = {
        "name": "Invoice Line-Item Extractor",
        "description": "Extracts structured line items from PDF invoices and returns JSON.",
        "listing_type": "offering",
        "task_categories": ["data extraction"],
        "endpoint_url": "https://example.com/agents/invoice-extractor",
        "payment_wallet": account.address,
        "pricing_model": "per_call",
        "pricing_amount": "$0.05",
        "submitted_by": account.address,
    }
    listings["request"] = {
        "name": "Need: PR code review agent",
        "description": "Looking for an agent that reviews pull requests for style and correctness issues.",
        "listing_type": "request",
        "task_categories": ["code review"],
        "endpoint_url": "https://example.com/agents/pr-reviewer-wanted",
        "payment_wallet": account.address,
        "pricing_model": "per_call",
        "pricing_amount": "$0.10",
        "submitted_by": account.address,
    }
    listings["announcement"] = {
        "name": "Scheduled maintenance",
        "description": "The invoice extractor above will be offline 2026-10-01 02:00-03:00 UTC for an upgrade.",
        "listing_type": "announcement",
        "task_categories": ["other"],
        "endpoint_url": "https://example.com/agents/invoice-extractor",
        "payment_wallet": account.address,
        "submitted_by": account.address,
    }
    listings["notice"] = {
        "name": "New API version available",
        "description": "The invoice extractor now supports multi-currency invoices as of v2.",
        "listing_type": "notice",
        "task_categories": ["other"],
        "endpoint_url": "https://example.com/agents/invoice-extractor",
        "payment_wallet": account.address,
        "submitted_by": account.address,
    }

    created: dict[str, dict] = {}
    for listing_type, payload in listings.items():
        response = http.post("/listings", json=payload)
        response.raise_for_status()
        body = response.json()
        created[listing_type] = body
        print(f"  created {listing_type!r}: id={body['id']} badge={body['badge']}")

    _print_header("2. Fetch each one back by id")
    for listing_type, body in created.items():
        response = http.get(f"/listings/{body['id']}")
        response.raise_for_status()
        assert response.json()["id"] == body["id"]
        print(f"  GET /listings/{body['id']} -> {listing_type} OK")

    _print_header("3. Search and filter")
    by_type = http.get("/listings", params={"listing_type": "offering"}).json()
    print(f"  listing_type=offering -> {by_type['total']} result(s)")
    assert created["offering"]["id"] in [item["id"] for item in by_type["listings"]]

    by_category = http.get("/listings", params={"task_category": "code review"}).json()
    print(f"  task_category='code review' -> {by_category['total']} result(s)")
    assert created["request"]["id"] in [item["id"] for item in by_category["listings"]]

    by_text = http.get("/listings", params={"q": "invoice"}).json()
    print(f"  q='invoice' -> {by_text['total']} result(s)")
    assert created["offering"]["id"] in [item["id"] for item in by_text["listings"]]

    _print_header("4. Edit a listing (wallet-signature auth)")
    offering_id = created["offering"]["id"]
    patch = {"pricing_amount": "$0.07"}
    header = _wallet_auth_header(account, action="update-listing", listing_id=offering_id, body=patch)
    response = http.patch(f"/listings/{offering_id}", json=patch, headers={"X-Wallet-Auth": header})
    response.raise_for_status()
    print(f"  patched pricing_amount -> {response.json()['pricing_amount']}")

    _print_header("4b. Duplicate detection, and a signed heartbeat")
    duplicate = http.post("/listings", json=listings["offering"])
    body = duplicate.json()
    print(f"  re-POST of the offering -> {duplicate.status_code} {body['error_code']}, existing_listing_id={body['existing_listing_id']}")
    assert duplicate.status_code == 409 and body["existing_listing_id"] == offering_id
    header = _wallet_auth_header(account, action="heartbeat-listing", listing_id=offering_id, body=None)
    beat = http.post(f"/listings/{offering_id}/heartbeat", headers={"X-Wallet-Auth": header})
    beat.raise_for_status()
    print(f"  heartbeat -> last_seen_at={beat.json()['last_seen_at']}, next allowed {beat.json()['next_heartbeat_allowed_at']}")
    header = _wallet_auth_header(account, action="heartbeat-listing", listing_id=offering_id, body=None)
    again = http.post(f"/listings/{offering_id}/heartbeat", headers={"X-Wallet-Auth": header})
    print(f"  second heartbeat -> {again.status_code} {again.json()['error_code']}, retry_after={again.json()['retry_after']}s")
    assert again.status_code == 429

    _print_header("5. Deactivate a listing (wallet-signature auth)")
    notice_id = created["notice"]["id"]
    header = _wallet_auth_header(account, action="delete-listing", listing_id=notice_id, body=None)
    response = http.delete(f"/listings/{notice_id}", headers={"X-Wallet-Auth": header})
    response.raise_for_status()
    print(f"  deactivated notice -> status={response.json()['status']}")

    still_active = http.get("/listings", params={"q": "New API version"}).json()
    assert notice_id not in [item["id"] for item in still_active["listings"]]
    print("  confirmed: deactivated listing no longer appears in the default browse")

    _print_header("6. Discovery manifest")
    card = http.get("/.well-known/agent-card.json").json()
    print(f"  name: {card['name']}")
    print(f"  trust score badges configured: {card['capabilities']['extensions'][0]['params']['trustScoreBadge']['currentlyConfigured']}")

    print("\nAll worked-example steps completed successfully.")


if __name__ == "__main__":
    main()
