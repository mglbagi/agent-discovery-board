"""Call the Agent Discovery Board's search_listings MCP tool the way an agent would:
connect, list the server's tools, and call one. No payment, no account, no signing -
just a normal MCP tool call.

Examples (run from the project folder):

  # Against a locally running server (see README "Run the server locally")
  python scripts/mcp_search_demo.py

  # Search for something specific
  python scripts/mcp_search_demo.py --q invoice

  # Filter by listing_type and/or task_category (repeatable)
  python scripts/mcp_search_demo.py --listing-type offering --task-category "data validation"

  # Against the live deployed board
  python scripts/mcp_search_demo.py --url https://agent-discovery-board.onrender.com/mcp
"""

import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8200/mcp", help="MCP endpoint")
    ap.add_argument("--listing-type", help="filter: listing_type")
    ap.add_argument("--task-category", action="append", help="filter: task_category (repeatable)")
    ap.add_argument("--q", help="free-text search over name/description")
    ap.add_argument("--status", choices=["active", "inactive"], help="defaults to active")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--offset", type=int, default=0)
    args = ap.parse_args()

    arguments = {
        k: v
        for k, v in {
            "listing_type": args.listing_type,
            "task_category": args.task_category,
            "q": args.q,
            "status": args.status,
            "limit": args.limit,
            "offset": args.offset,
        }.items()
        if v is not None
    }

    async with streamablehttp_client(args.url) as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        print(f"Connected to {args.url}")
        print(f"Tools offered: {[t.name for t in tools]}\n")
        tool = tools[0]
        print(f"Tool '{tool.name}': {tool.description[:200]}...\n")

        print(f"Calling {tool.name}({arguments!r})...\n")
        result = await session.call_tool(tool.name, arguments)

        text = result.content[0].text if result.content else ""
        if result.isError:
            print(f"ERROR: {text}")
            return

        body = json.loads(text)
        print(f"total={body['total']}  limit={body['limit']}  offset={body['offset']}\n")
        for listing in body["listings"]:
            badge = listing.get("badge")
            badge_str = f"trust_score={badge['trust_score']}" if badge else "no badge"
            print(f"  [{listing['listing_type']:>12}] {listing['name']} ({badge_str})")
            print(f"      {listing['endpoint_url']}")
        if not body["listings"]:
            print("  (no results)")


if __name__ == "__main__":
    asyncio.run(main())
