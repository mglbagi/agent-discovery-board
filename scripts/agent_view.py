"""What an agent sees: the board's raw request/response JSON for the main flows - create,
duplicate, heartbeat, wrong signer, cursor paging, errors, the manifest and the MCP tool.

Safe by construction (see scripts/demo_safety.py): every listing it creates is a temporary
`test-` listing with an endpoint under example.invalid, it refuses a non-local URL unless
you pass --allow-production, and it deactivates everything it created even if it fails.

    python scripts/agent_view.py                                   # a local server
    python scripts/agent_view.py --url https://... --allow-production
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import demo_safety  # noqa: E402
from demo_safety import DemoSession, demo_endpoint, demo_name, new_marker, signed_header  # noqa: E402
from eth_account import Account  # noqa: E402

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_PAYEE = "38Fmaf3MWTR6AWPWrtrdXoqn6iqfVcUBHMFRhiUAEjFb"
SOLANA_NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"


def show(title, response, keys=None, extra_headers=()):
    print(f"\n### {title}")
    print(f"HTTP {response.status_code}", *[f"{h}: {response.headers[h]}" for h in extra_headers if h in response.headers])
    body = response.json()
    if keys:
        body = {k: body[k] for k in keys if k in body}
    print(json.dumps(body, indent=2))


def run(http, base_url: str, demo: DemoSession) -> None:
    owner, other = Account.create(), Account.create()
    marker = new_marker()
    offering = {
        "name": demo_name("verifier", marker),
        "description": "Verification for AI agent work.",
        "listing_type": "offering",
        "task_categories": ["data validation"],
        "endpoint_url": demo_endpoint(marker, "/verify/schema"),
        "payment_wallet": owner.address,
        "pricing_model": "per_call",
        "pricing_amount": "$0.02 per verification; $0.01 per score lookup",
        "payment_options": [
            {"network": "eip155:8453", "asset": BASE_USDC, "pay_to": owner.address, "amount": "0.02", "unit": "per_verification"},
            {"network": SOLANA_NETWORK, "asset": SOLANA_USDC, "pay_to": SOLANA_PAYEE, "amount": "0.02", "unit": "per_verification"},
        ],
        "submitted_by": owner.address,
    }

    created = demo.create(offering, owner)
    show("1. POST /listings (a test- listing, with payment_options) - created", created)
    lid = created.json()["id"]

    show("2. POST /listings again, same endpoint + submitted_by (hostile, different body)", demo.create({**offering, "name": demo_name("takeover", marker)}, owner))
    show("2b. ...and the listing is untouched", http.get(f"/listings/{lid}"), keys=["id", "name", "test", "expires_at", "updated_at", "last_seen_at"])

    bad = {**offering, "name": demo_name("bad", marker), "endpoint_url": demo_endpoint(marker, "/other")}
    bad["payment_options"] = [{**offering["payment_options"][0], "pay_to": SOLANA_PAYEE}]
    show("3. POST with a Solana address on an EVM network", demo.create(bad, owner))

    def heartbeat(account):
        return http.post(f"/listings/{lid}/heartbeat", headers=signed_header(account, action="heartbeat-listing", listing_id=lid))

    show("4. POST /listings/{id}/heartbeat signed by the owner", heartbeat(owner))
    show("5. heartbeat again (too early)", heartbeat(owner), extra_headers=["retry-after"])
    show("6. heartbeat signed by a different wallet", heartbeat(other))
    show("7. heartbeat with no signature", http.post(f"/listings/{lid}/heartbeat"))

    second = demo.create(
        {**offering, "name": demo_name("second", marker), "endpoint_url": demo_endpoint(marker, "/second"),
         "submitted_by": other.address, "payment_wallet": other.address, "payment_options": []},
        other,
    ).json()

    hidden = http.get("/listings", params={"q": marker}).json()
    print(f"\n### 8. test- listings are hidden by default: GET /listings?q={marker} -> total {hidden['total']}")
    page = http.get("/listings", params={"q": marker, "include_test": "true", "limit": 1}).json()
    print("### 8b. ...and visible with include_test=true - newest activity first, with a cursor")
    slim = {**page, "listings": [{k: l[k] for k in ("id", "name", "test", "expires_at", "last_activity_at", "stale")} for l in page["listings"]]}
    print(json.dumps(slim, indent=2))
    page2 = http.get("/listings", params={"q": marker, "include_test": "true", "limit": 1, "cursor": page["next_cursor"]}).json()
    print("next page ->", [l["name"] for l in page2["listings"]], "next_cursor:", page2["next_cursor"])
    show("9. bad cursor", http.get("/listings", params={"cursor": "garbage"}))
    show("10. unknown listing", http.get("/listings/00000000-0000-4000-8000-000000000000"))

    params = http.get("/.well-known/agent-card.json").json()["capabilities"]["extensions"][0]["params"]
    print("\n### 11. manifest: signingSpec.worked_example.update_listing")
    print(json.dumps(params["signingSpec"]["worked_example"]["update_listing"], indent=2))
    print("\n### 12. manifest: errors.codes")
    print(json.dumps([{k: c[k] for k in ("error_code", "http_status", "retryable")} for c in params["errors"]["codes"]]))
    print("\n### 13. manifest: testListings + freshness + duplicateDetection")
    print(json.dumps({k: params[k] for k in ("testListings", "freshness", "duplicateDetection")}, indent=2))

    async def mcp() -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(f"{base_url.rstrip('/')}/mcp/") as (r, w, _), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool("search_listings", {"q": marker, "include_test": True, "limit": 1})
            body = json.loads(res.content[0].text)
            print(f"\n### 14. MCP search_listings include_test=true (isError: {res.isError}) -> {len(body['listings'])} listing(s), next_cursor present: {bool(body['next_cursor'])}")
            hidden_res = await session.call_tool("search_listings", {"q": marker})
            print(f"### 14b. MCP search_listings without include_test -> total {json.loads(hidden_res.content[0].text)['total']}")
            bad = await session.call_tool("search_listings", {"cursor": "garbage"})
            print(f"\n### 15. MCP error (isError: {bad.isError})")
            print(json.dumps(json.loads(bad.content[0].text), indent=2))

    asyncio.run(mcp())
    print("\n### 16. /llms.txt (first 25 lines)")
    print("\n".join(http.get("/llms.txt").text.splitlines()[:25]))
    assert second["test"] is True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8200", help="base URL of a running instance")
    ap.add_argument("--allow-production", action="store_true", help="permit a non-local URL")
    args = ap.parse_args(argv)
    demo_safety.require_safe_target(args.url, args.allow_production)

    http = demo_safety.make_client(args.url)
    with DemoSession(http) as demo:
        run(http, args.url, demo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
