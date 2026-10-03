"""OPERATOR-ONLY, one-time: point stale imported listings' template_url at the board itself.

POST /admin/import (and scripts/bulk_import_listings.py) set an imported listing's
template_url to the board's own GET /listings/{id}/template - but only for the listings a
batch contains. A listing the source stopped supplying is marked stale and is, by
definition, not in the batch, so it kept the source's off-board link (a GitHub file, in
practice). This script gives those the board's own link too, so no imported listing's
template depends on a file hosted elsewhere.

It changes exactly one column, template_url, and nothing else (not updated_at, not the sync
timestamps). A row is touched only if ALL of these hold, re-checked inside the UPDATE itself:
  * it is an imported listing (source is set) that is still unclaimed - a claimed listing's
    content is its owner's, and a re-sync leaves it alone, so does this;
  * it is stale because its source stopped supplying it (missing_from_source_since is set);
  * it has an output_schema (the board's template route answers 404 without one);
  * its template_url does not already start with <SERVICE_BASE_URL>/listings/.

Safe by construction:
  * DRY RUN by default: counts and lists what would change, writes nothing.
  * --apply requires --expect-count N (the number the dry run reported); if the database no
    longer matches it, nothing is written. One transaction: all or nothing.
  * audited: every applied run appends one JSON line (with each listing's id and its
    before/after template_url) to admin_changes.log, which .gitignore already excludes.
  * prints the target database host, never credentials.

  python scripts/admin_fix_template_urls.py --board-url https://<board>                      # dry run
  python scripts/admin_fix_template_urls.py --board-url https://<board> --apply --expect-count 170 --yes
"""

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402,F401  (loads .env)
from app.core import import_sync  # noqa: E402
import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_GUARD = 0, 1, 2

# The same conditions in the preview and in the UPDATE, so what the dry run counts is what
# --apply may touch.
_WHERE = (
    "source IS NOT NULL AND claimed = FALSE AND missing_from_source_since IS NOT NULL "
    "AND output_schema IS NOT NULL "
    "AND (template_url IS NULL OR left(template_url, length(%(prefix)s)) <> %(prefix)s)"
)


class AdminError(Exception):
    def __init__(self, message: str, code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.code = code


def _target(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.hostname}{parsed.path}"


def board_prefix(args: argparse.Namespace) -> str:
    try:
        return import_sync.resolve_board_url(args.board_url, allow_local=args.allow_local_board_url) + "/listings/"
    except ValueError as exc:
        raise AdminError(str(exc)) from exc


def _append_log(log_file: str, entry: dict[str, Any]) -> None:
    with open(log_file, "a", encoding="utf-8") as log:
        log.write(json.dumps(entry, default=str, sort_keys=True) + "\n")
    print(f"Logged to {log_file}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--board-url", default=None,
        help="the board's public URL the new links are built from (default: SERVICE_BASE_URL; a local address is "
        "refused unless --allow-local-board-url)",
    )
    ap.add_argument("--allow-local-board-url", action="store_true", help="accept a localhost board URL (a local board)")
    ap.add_argument("--apply", action="store_true", help="write the change (default is a dry run)")
    ap.add_argument("--expect-count", type=int, default=None, help="with --apply: the number of rows the dry run reported")
    ap.add_argument("--yes", action="store_true", help="with --apply, skip the confirmation prompt")
    ap.add_argument("--log-file", default="admin_changes.log", help="audit log, one JSON line per applied change")
    ap.add_argument("--show", type=int, default=5, help="how many example rows to print (default 5)")
    args = ap.parse_args(argv)
    try:
        return _run(args)
    except AdminError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code


def _run(args: argparse.Namespace) -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise AdminError("DATABASE_URL is not set.")
    prefix = board_prefix(args)
    print(f"Target database: {_target(url)}")
    print(f"Board link prefix: {prefix}{{id}}/template")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN (nothing will be written)'}")
    if args.apply and args.expect_count is None:
        raise AdminError("--apply needs --expect-count N (the number the dry run reported).", EXIT_GUARD)

    params = {"prefix": prefix}
    with psycopg.connect(url, row_factory=dict_row) as conn:
        rows = conn.execute(
            f"SELECT id, name, template_url FROM listings WHERE {_WHERE} ORDER BY id", params
        ).fetchall()
        print(f"\n{len(rows)} stale, unclaimed imported listing(s) with an off-board template_url.")
        for row in rows[: args.show]:
            print(f"  {row['id']}  {row['name']!r}\n      {row['template_url']}  ->  {prefix}{row['id']}/template")
        if len(rows) > args.show:
            print(f"  ... and {len(rows) - args.show} more")

        if not args.apply:
            print("\nDRY RUN: nothing written. Re-run with --apply --expect-count N to write.")
            return EXIT_OK
        if len(rows) != args.expect_count:
            raise AdminError(
                f"expected {args.expect_count} row(s) but {len(rows)} match now; nothing written.", EXIT_GUARD
            )
        if rows and not args.yes and input(f"\nType the count ({len(rows)}) to confirm: ").strip() != str(len(rows)):
            raise AdminError("confirmation did not match; nothing written.", EXIT_GUARD)

        before = {r["id"]: r["template_url"] for r in rows}
        # Only template_url is set; the WHERE is re-evaluated here, inside the transaction,
        # and the row set must still be exactly the one that was shown.
        updated = conn.execute(
            f"UPDATE listings SET template_url = %(prefix)s || id::text || '/template' WHERE {_WHERE} "
            "RETURNING id, template_url",
            params,
        ).fetchall()
        if {r["id"] for r in updated} != set(before):
            conn.rollback()
            raise AdminError("the matching rows changed between the preview and the write; rolled back.", EXIT_GUARD)
        conn.commit()

    print(f"\nUpdated {len(updated)} listing(s).")
    _append_log(
        args.log_file,
        {
            "action": "fix-template-urls",
            "ts": datetime.now(timezone.utc).isoformat(),
            "operator": getpass.getuser(),
            "db": _target(url),
            "changed_column": "template_url",
            "count": len(updated),
            "listings": [
                {"id": r["id"], "template_url_before": before[r["id"]], "template_url_after": r["template_url"]}
                for r in sorted(updated, key=lambda r: r["id"])
            ],
        },
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
