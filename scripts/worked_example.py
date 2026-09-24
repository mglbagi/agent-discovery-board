"""Worked example: create one listing of every listing_type, fetch them back, exercise
search and filters, edit, heartbeat and deactivate with real wallet signatures.

Safe by construction (see scripts/demo_safety.py): every listing is a temporary `test-`
listing (hidden from default browse and search, purged by the board after its TTL) with an
endpoint under example.invalid; a non-local URL is refused unless you pass
--allow-production; and everything created is deactivated at the end, even on failure.

    python scripts/worked_example.py                       # a local server
    python scripts/worked_example.py --url https://your-service.onrender.com --allow-production
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import demo_safety  # noqa: E402
from demo_safety import DemoSession, demo_endpoint, demo_name, new_marker, signed_header  # noqa: E402
from eth_account import Account  # noqa: E402


def _print_header(title: str) -> None:
    print(f"\n{'=' * 10} {title} {'=' * 10}")


def run(http, demo: DemoSession, account) -> None:
    marker = new_marker()
    endpoint = demo_endpoint(marker, "/agents/invoice-extractor")
    # Test listings are hidden from browse/search unless asked for; scope every query to this run.
    seen = {"include_test": "true", "q": marker}

    _print_header("1. Create one listing of each listing_type")
    listings = {
        "offering": {
            "name": demo_name("invoice-extractor", marker),
            "description": "Extracts structured line items from PDF invoices and returns JSON.",
            "listing_type": "offering",
            "task_categories": ["data extraction"],
            "endpoint_url": endpoint,
            "payment_wallet": account.address,
            "pricing_model": "per_call",
            "pricing_amount": "$0.05",
            "submitted_by": account.address,
        },
        "request": {
            "name": demo_name("pr-review-wanted", marker),
            "description": "Looking for an agent that reviews pull requests for style and correctness issues.",
            "listing_type": "request",
            "task_categories": ["code review"],
            "endpoint_url": demo_endpoint(marker, "/agents/pr-reviewer-wanted"),
            "payment_wallet": account.address,
            "pricing_model": "per_call",
            "pricing_amount": "$0.10",
            "submitted_by": account.address,
        },
        # Announcements and notices may repeat an endpoint_url and submitted_by (only offerings are
        # duplicate-guarded), so they share the offering's URL, as they would in practice.
        "announcement": {
            "name": demo_name("maintenance", marker),
            "description": "The invoice extractor above will be offline 2026-10-01 02:00-03:00 UTC for an upgrade.",
            "listing_type": "announcement",
            "task_categories": ["other"],
            "endpoint_url": endpoint,
            "payment_wallet": account.address,
            "submitted_by": account.address,
        },
        "notice": {
            "name": demo_name("new-api-version", marker),
            "description": "The invoice extractor now supports multi-currency invoices as of v2.",
            "listing_type": "notice",
            "task_categories": ["other"],
            "endpoint_url": endpoint,
            "payment_wallet": account.address,
            "submitted_by": account.address,
        },
    }
    created: dict[str, dict] = {}
    for listing_type, payload in listings.items():
        response = demo.create(payload, account)
        response.raise_for_status()
        created[listing_type] = response.json()
        body = created[listing_type]
        print(f"  created {listing_type!r}: id={body['id']} test={body['test']} expires_at={body['expires_at']} badge={body['badge']}")

    _print_header("2. Fetch each one back by id")
    for listing_type, body in created.items():
        response = http.get(f"/listings/{body['id']}")
        response.raise_for_status()
        assert response.json()["id"] == body["id"]
        print(f"  GET /listings/{body['id']} -> {listing_type} OK")

    _print_header("3. Search and filter (test- listings are hidden unless include_test=true)")
    hidden = http.get("/listings", params={"q": marker}).json()
    print(f"  without include_test -> {hidden['total']} result(s): demo data stays out of normal browsing")
    assert hidden["total"] == 0

    by_type = http.get("/listings", params={**seen, "listing_type": "offering"}).json()
    print(f"  listing_type=offering -> {by_type['total']} result(s)")
    assert [i["id"] for i in by_type["listings"]] == [created["offering"]["id"]]

    by_category = http.get("/listings", params={**seen, "task_category": "code review"}).json()
    print(f"  task_category='code review' -> {by_category['total']} result(s)")
    assert [i["id"] for i in by_category["listings"]] == [created["request"]["id"]]

    by_text = http.get("/listings", params=seen).json()
    print(f"  q={marker!r} -> {by_text['total']} result(s)")
    assert by_text["total"] == 4

    _print_header("4. Edit a listing (wallet-signature auth)")
    offering_id = created["offering"]["id"]
    patch = {"pricing_amount": "$0.07"}
    response = http.patch(
        f"/listings/{offering_id}", json=patch, headers=signed_header(account, action="update-listing", listing_id=offering_id, body=patch)
    )
    response.raise_for_status()
    print(f"  patched pricing_amount -> {response.json()['pricing_amount']}")

    _print_header("4b. Duplicate detection (offerings only), and a signed heartbeat")
    duplicate = demo.create(listings["offering"], account)
    body = duplicate.json()
    print(f"  re-POST of the offering -> {duplicate.status_code} {body['error_code']}, existing_listing_id={body['existing_listing_id']}")
    assert duplicate.status_code == 409 and body["existing_listing_id"] == offering_id
    again = demo.create({**listings["announcement"], "name": demo_name("maintenance-2", marker)}, account)
    print(f"  a second announcement at the same endpoint -> {again.status_code} (only offerings are guarded)")
    assert again.status_code == 201

    beat = http.post(f"/listings/{offering_id}/heartbeat", headers=signed_header(account, action="heartbeat-listing", listing_id=offering_id))
    beat.raise_for_status()
    print(f"  heartbeat -> last_seen_at={beat.json()['last_seen_at']}, next allowed {beat.json()['next_heartbeat_allowed_at']}")
    second = http.post(f"/listings/{offering_id}/heartbeat", headers=signed_header(account, action="heartbeat-listing", listing_id=offering_id))
    print(f"  second heartbeat -> {second.status_code} {second.json()['error_code']}, retry_after={second.json()['retry_after']}s")
    assert second.status_code == 429

    _print_header("5. Deactivate a listing (wallet-signature auth)")
    notice_id = created["notice"]["id"]
    response = http.delete(f"/listings/{notice_id}", headers=signed_header(account, action="delete-listing", listing_id=notice_id))
    response.raise_for_status()
    print(f"  deactivated notice -> status={response.json()['status']}")
    still = http.get("/listings", params=seen).json()
    assert notice_id not in [item["id"] for item in still["listings"]]
    print("  confirmed: a deactivated listing no longer appears in the default browse")

    _print_header("6. Discovery manifest")
    card = http.get("/.well-known/agent-card.json").json()
    params = card["capabilities"]["extensions"][0]["params"]
    print(f"  name: {card['name']}")
    print(f"  trust score badges configured: {params['trustScoreBadge']['currentlyConfigured']}")
    print(f"  test listings: prefix {params['testListings']['prefix']!r}, purged after {params['testListings']['ttlHours']:g}h")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8200", help="base URL of a running instance")
    ap.add_argument("--allow-production", action="store_true", help="permit a non-local URL")
    args = ap.parse_args(argv)
    demo_safety.require_safe_target(args.url, args.allow_production)

    account = Account.create()
    print(f"Using a fresh throwaway wallet for this run: {account.address}")
    http = demo_safety.make_client(args.url)
    with DemoSession(http) as demo:
        run(http, demo, account)
    print("\nAll worked-example steps completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
