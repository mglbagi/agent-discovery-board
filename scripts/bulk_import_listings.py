"""OPERATOR-ONLY: bulk-import listings from a third-party directory (e.g. the x402
bazaar) into the board, upserting by (source, endpoint_url) so a re-sync updates an
already-imported listing instead of duplicating it, and marks listings that have
disappeared from the source as stale (see app/core/imports.py).

Every imported listing starts unclaimed (claimed: false, submitted_by set to a
placeholder no one can sign for) until its real owner proves control of payment_wallet
via POST /listings/{id}/claim - this script never sets submitted_by to anything a human
controls, and never touches a listing's content once it has been claimed (only
last_synced_at and missing_from_source_since).

Safe by construction:
  * DRY RUN by default: prints what would happen and writes nothing. Pass --apply.
  * validated: every record must pass the same validation as POST /listings (plus
    source/source_url) before ANYTHING is written - one bad record aborts the whole
    batch rather than partially importing.
  * a do-not-import list is always honored: a listing someone asked removed
    (POST /listings/{id}/remove-imported) is never recreated by a later sync, silently
    skipped and counted separately.
  * claimed listings are never overwritten: a re-sync only refreshes last_synced_at and
    clears missing_from_source_since for them, never their content.
  * audited: appends one JSON line per run to the log file (default
    ./bulk_import_changes.log).

Input: a JSON file holding a list of records, each shaped like a POST /listings body
plus an optional source_url:
  [
    {
      "name": "...", "description": "...", "task_categories": ["other"],
      "endpoint_url": "https://...", "payment_wallet": "0x...",
      "pricing_model": "per_call", "pricing_amount": "$0.01",
      "source_url": "https://bazaar.example.com/agents/123"
    },
    ...
  ]
listing_type defaults to "verification_profile" if omitted.

Examples:

  # See what a sync would do (nothing is written):
  python scripts/bulk_import_listings.py --source x402_bazaar --file records.json

  # Actually do it:
  python scripts/bulk_import_listings.py --source x402_bazaar --file records.json --apply
"""

import argparse
import getpass
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402,F401  (loads .env)
from app.core import db  # noqa: E402
from app.core.imports import ImportRecordError, build_import_row  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_GUARD = 0, 1, 2


class AdminError(Exception):
    def __init__(self, message: str, code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.code = code


def _target(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.hostname}{parsed.path}"


def _load_records(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdminError(f"could not read {path} as JSON: {exc}") from exc
    if not isinstance(data, list):
        raise AdminError(f"{path} must contain a JSON array of records.")
    return data


def _append_log(args: argparse.Namespace, entry: dict[str, Any]) -> None:
    with open(args.log_file, "a", encoding="utf-8") as log:
        log.write(json.dumps(entry, default=str, sort_keys=True) + "\n")
    print(f"Logged to {args.log_file}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", required=True, help="source identifier, e.g. 'x402_bazaar'")
    ap.add_argument("--file", required=True, type=Path, help="JSON file: a list of records to import")
    ap.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    ap.add_argument(
        "--yes", action="store_true", help="with --apply, skip the type-SOURCE-to-confirm prompt"
    )
    ap.add_argument(
        "--log-file", default="bulk_import_changes.log", help="audit log, one JSON line per run"
    )
    args = ap.parse_args(argv)

    try:
        return _run(args)
    except AdminError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code


def _run(args: argparse.Namespace) -> int:
    records = _load_records(args.file)
    print(f"Target database: {_target(db.DATABASE_URL)}")
    print(f"Source: {args.source}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN (nothing will be written)'}")
    print(f"{len(records)} record(s) in {args.file}")

    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            raise AdminError(f"record #{i}: expected an object, got {type(record).__name__}.", EXIT_GUARD)
        try:
            rows.append(build_import_row(record, source=args.source, now=now))
        except ImportRecordError as exc:
            raise AdminError(f"record #{i} ({record.get('name', '?')!r}) is invalid: {exc}", EXIT_GUARD) from exc
    print(f"All {len(rows)} record(s) validated.")

    db.init_db()  # ensures the schema (and its do_not_import table) exists even on a fresh DB

    skipped: list[dict[str, Any]] = []
    importable: list[dict[str, Any]] = []
    for row in rows:
        if db.is_in_do_not_import(args.source, row["endpoint_url"]):
            skipped.append(row)
        else:
            importable.append(row)

    if skipped:
        print(f"\n{len(skipped)} record(s) skipped (on the do-not-import list):")
        for row in skipped:
            print(f"  {row['name']!r}  {row['endpoint_url']}")

    seen_endpoint_urls = [row["endpoint_url"] for row in rows]  # the full sync, including skipped
    would_mark_missing = db.preview_missing_from_source(args.source, seen_endpoint_urls)
    if would_mark_missing:
        print(f"\n{len(would_mark_missing)} previously-imported listing(s) from this source are no longer "
              f"present and would be marked missing (stale):")
        for item in would_mark_missing:
            print(f"  {item['id']}  {item['name']!r}")

    if not importable:
        print("\nNothing to import.")
        if not args.apply:
            print("DRY RUN: nothing written.")
            return EXIT_OK

    if not args.apply:
        print(f"\n{len(importable)} record(s) would be inserted or updated.")
        print("\nDRY RUN: nothing written. Re-run with --apply to write these changes.")
        return EXIT_OK

    if not args.yes and input(f"\nType the source ({args.source}) to confirm: ").strip() != args.source:
        raise AdminError("confirmation did not match; nothing written.", EXIT_GUARD)

    inserted, updated = [], []
    for row in importable:
        result, was_inserted = db.import_upsert(row)
        (inserted if was_inserted else updated).append(result)

    newly_missing = db.mark_missing_from_source(args.source, seen_endpoint_urls, now)

    print(f"\nInserted {len(inserted)}, updated {len(updated)}, newly marked missing {len(newly_missing)}, "
          f"skipped (do-not-import) {len(skipped)}.")
    _append_log(
        args,
        {
            "ts": now.isoformat(),
            "operator": getpass.getuser(),
            "db": _target(db.DATABASE_URL),
            "source": args.source,
            "inserted": [{"id": r["id"], "name": r["name"], "endpoint_url": r["endpoint_url"]} for r in inserted],
            "updated": [{"id": r["id"], "name": r["name"], "endpoint_url": r["endpoint_url"]} for r in updated],
            "newly_missing": newly_missing,
            "skipped_do_not_import": [{"name": r["name"], "endpoint_url": r["endpoint_url"]} for r in skipped],
        },
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
