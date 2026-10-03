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
  * best-effort, not all-or-nothing: each record is validated independently (plus
    source/source_url) - an invalid record is REJECTED and reported, not a reason to
    abort the rest of a multi-thousand-record file. Nothing is written for any record
    until every record in the file has been checked.
  * a do-not-import list is always honored: a listing someone asked removed
    (POST /listings/{id}/remove-imported) is never recreated by a later sync, silently
    skipped and counted separately.
  * claimed listings are never overwritten: a re-sync only refreshes last_synced_at and
    clears missing_from_source_since for them, never their content.
  * audited: appends one JSON line per run to the log file (default
    ./bulk_import_changes.log).

Input: a JSON file holding a list of records, OR one JSON object per line (.jsonl) -
either way each record is shaped like a POST /listings body (plus verification,
template_url, source_url) - see app/core/imports.py's build_import_row for the exact
fields and fallbacks (a record's own payment_wallet, or an EVM/Solana payment_options
entry's pay_to; a record's own source, or --source, or _import.source). Unsupported
payment_options entries (any network this board doesn't represent, i.e. not eip155/
solana, or a malformed address) are dropped rather than failing the record, as long as
at least one usable payment_wallet remains.

Examples:

  # See what a sync would do (nothing is written) - source comes from each record:
  python scripts/bulk_import_listings.py --file records.jsonl

  # Force one source for every record, overriding anything in the file:
  python scripts/bulk_import_listings.py --source x402_bazaar --file records.json

  # Actually do it:
  python scripts/bulk_import_listings.py --file records.jsonl --apply
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
from app.core import import_sync  # noqa: E402
from app.core.models import is_evm_address  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_GUARD = 0, 1, 2


class AdminError(Exception):
    def __init__(self, message: str, code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.code = code


def _target(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.hostname}{parsed.path}"


def _load_records(path: Path) -> list[Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AdminError(f"could not read {path}: {exc}") from exc
    try:
        return import_sync.parse_records(text, str(path))
    except import_sync.ImportInputError as exc:
        raise AdminError(str(exc)) from exc


def _append_log(args: argparse.Namespace, entry: dict[str, Any]) -> None:
    with open(args.log_file, "a", encoding="utf-8") as log:
        log.write(json.dumps(entry, default=str, sort_keys=True) + "\n")
    print(f"Logged to {args.log_file}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--source", default=None,
        help="source identifier, e.g. 'x402_bazaar' - overrides every record's own source/_import.source if given",
    )
    ap.add_argument(
        "--board-url", default=None,
        help="the board's public URL, which imported listings' template_url is built from "
        "(default: SERVICE_BASE_URL; a local address is refused unless --allow-local-board-url)",
    )
    ap.add_argument("--allow-local-board-url", action="store_true", help="accept a localhost board URL (a local board)")
    ap.add_argument("--file", required=True, type=Path, help="JSON array or .jsonl file of records to import")
    ap.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    ap.add_argument("--yes", action="store_true", help="with --apply, skip the confirmation prompt")
    ap.add_argument("--log-file", default="bulk_import_changes.log", help="audit log, one JSON line per run")
    ap.add_argument(
        "--show-rejected", type=int, default=50,
        help="print at most this many rejected records' reasons (default 50; they are all still counted)",
    )
    args = ap.parse_args(argv)

    try:
        return _run(args)
    except AdminError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code


def _run(args: argparse.Namespace) -> int:
    records = _load_records(args.file)
    now = datetime.now(timezone.utc)
    print(f"Target database: {_target(db.DATABASE_URL)}")
    print(f"Source: {args.source or '(from each record)'}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN (nothing will be written)'}")
    print(f"{len(records)} record(s) in {args.file}")

    template_base = None
    if args.apply:
        try:
            template_base = import_sync.resolve_board_url(args.board_url, allow_local=args.allow_local_board_url)
        except ValueError as exc:
            raise AdminError(str(exc)) from exc
        print(f"Template links: {template_base}/listings/{{id}}/template")

    db.init_db()  # ensures the schema (and its do_not_import table) exists even on a fresh DB
    plan = import_sync.plan_sync(records, args.source, now)
    rows, rejected, stats = plan.rows, plan.rejected, plan.stats

    print(f"\n{len(rows)} record(s) valid, {len(rejected)} rejected.")
    if rejected:
        for item in rejected[: args.show_rejected]:
            print(f"  #{item['index']} {item['name']!r}: {item['reason']}")
        if len(rejected) > args.show_rejected:
            print(f"  ... and {len(rejected) - args.show_rejected} more (see --show-rejected to print more)")

    if stats["dropped_payment_options"]:
        print(f"\n{stats['dropped_payment_options']} payment_options entry/entries dropped across the valid "
              f"records (network not eip155/solana, or a malformed address) - the records themselves still "
              f"import with their remaining options.")
    if stats["derived_payment_wallet"]:
        print(f"{stats['derived_payment_wallet']} record(s) had no payment_wallet of their own; one was derived "
              f"from a payment_option's pay_to.")
    solana_only = sum(1 for r in rows if not is_evm_address(r["payment_wallet"]))
    if solana_only:
        print(f"{solana_only} record(s) have a Solana-only payment_wallet (importable, but not claimable/"
              f"self-removable yet - see the README's known limitations).")
    if plan.duplicates_in_batch:
        print(f"{plan.duplicates_in_batch} record(s) repeat an endpoint already in this batch (the last one wins).")

    skipped = plan.skipped
    if skipped:
        print(f"\n{len(skipped)} record(s) skipped (on the do-not-import list):")
        for row in skipped[:20]:
            print(f"  {row['name']!r}  {row['endpoint_url']}")
        if len(skipped) > 20:
            print(f"  ... and {len(skipped) - 20} more")

    total_missing = plan.total_missing
    if total_missing:
        print(f"\n{total_missing} previously-imported listing(s) are no longer present in this sync and would be "
              f"marked missing (stale):")
        for source, items in plan.would_mark_missing.items():
            for item in items[:20]:
                print(f"  [{source}] {item['id']}  {item['name']!r}")

    if not args.apply:
        print(f"\n{len(plan.importable)} record(s) would be inserted or updated.")
        print("\nDRY RUN: nothing written. Re-run with --apply to write these changes.")
        return EXIT_OK

    sources = sorted(plan.sources_in_sync)
    if plan.importable or total_missing:
        confirm_phrase = sources[0] if len(sources) == 1 else "IMPORT"
        prompt = f"\nType the source ({confirm_phrase}) to confirm: " if len(sources) == 1 else (
            f"\n{len(sources)} different sources in this batch: {', '.join(sources)}. Type IMPORT to confirm: "
        )
        if not args.yes and input(prompt).strip() != confirm_phrase:
            raise AdminError("confirmation did not match; nothing written.", EXIT_GUARD)

    outcome = import_sync.apply_plan(plan, now, template_base=template_base)
    inserted, updated, newly_missing = outcome.inserted, outcome.updated, outcome.newly_missing

    print(f"\nInserted {len(inserted)}, updated {len(updated)}, newly marked missing {len(newly_missing)}, "
          f"skipped (do-not-import) {len(skipped)}, rejected {len(rejected)}.")
    _append_log(
        args,
        {
            "ts": now.isoformat(),
            "operator": getpass.getuser(),
            "db": _target(db.DATABASE_URL),
            "sources": sources,
            "inserted": [{"id": r["id"], "name": r["name"], "endpoint_url": r["endpoint_url"]} for r in inserted],
            "updated": [{"id": r["id"], "name": r["name"], "endpoint_url": r["endpoint_url"]} for r in updated],
            "newly_missing": newly_missing,
            "skipped_do_not_import": [{"name": r["name"], "endpoint_url": r["endpoint_url"]} for r in skipped],
            "rejected_count": len(rejected),
        },
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
