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
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402,F401  (loads .env)
from app.core import db  # noqa: E402
from app.core.imports import ImportRecordError, build_import_row  # noqa: E402
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
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AdminError(f"could not parse {path} as a JSON array: {exc}") from exc
        if not isinstance(data, list):
            raise AdminError(f"{path} must contain a JSON array of records.")
        return data
    # .jsonl: one JSON object per non-blank line.
    records: list[Any] = []
    for i, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise AdminError(f"{path}, line {i + 1}: not valid JSON: {exc}") from exc
    return records


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


def _validate_all(
    records: list[Any], source: str | None, now: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Every record, independently - returns (valid rows, rejected [{index, name, reason}],
    stats). Never raises for a single bad record; a non-object entry is just another
    rejection. stats tracks non-fatal payment-data adaptations (app/core/imports.py's
    _filter_payment_options / _resolve_payment_wallet) - informational, these records
    still import."""
    rows, rejected = [], []
    stats = {"dropped_payment_options": 0, "derived_payment_wallet": 0}
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            rejected.append({"index": i, "name": None, "reason": f"expected an object, got {type(record).__name__}"})
            continue
        try:
            row = build_import_row(record, source=source, now=now)
        except ImportRecordError as exc:
            rejected.append({"index": i, "name": record.get("name"), "reason": str(exc)})
            continue
        rows.append(row)
        stats["dropped_payment_options"] += max(
            0, len(record.get("payment_options") or []) - len(row["payment_options"])
        )
        if record.get("payment_wallet") is None:
            stats["derived_payment_wallet"] += 1
    return rows, rejected, stats


def _run(args: argparse.Namespace) -> int:
    records = _load_records(args.file)
    now = datetime.now(timezone.utc)
    print(f"Target database: {_target(db.DATABASE_URL)}")
    print(f"Source: {args.source or '(from each record)'}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN (nothing will be written)'}")
    print(f"{len(records)} record(s) in {args.file}")

    rows, rejected, stats = _validate_all(records, args.source, now)
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

    db.init_db()  # ensures the schema (and its do_not_import table) exists even on a fresh DB

    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_source[row["source"]].append(row)
    # An explicit --source is still "in this sync" even if it ended up with zero valid
    # rows (an empty file, or every record for it rejected) - a sync like that should
    # still report (and, on --apply, mark) anything previously imported for it as missing.
    sources_in_sync = set(by_source) | ({args.source} if args.source else set())

    skipped: list[dict[str, Any]] = []
    importable: list[dict[str, Any]] = []
    would_mark_missing_by_source: dict[str, list[dict[str, Any]]] = {}
    for source in sources_in_sync:
        source_rows = by_source.get(source, [])
        blocked_keys = db.do_not_import_keys(source, [r["endpoint_url"] for r in source_rows])
        for row in source_rows:
            if db.normalize_endpoint_url(row["endpoint_url"]) in blocked_keys:
                skipped.append(row)
            else:
                importable.append(row)
        would_mark_missing_by_source[source] = db.preview_missing_from_source(
            source, [r["endpoint_url"] for r in source_rows]
        )

    if skipped:
        print(f"\n{len(skipped)} record(s) skipped (on the do-not-import list):")
        for row in skipped[:20]:
            print(f"  {row['name']!r}  {row['endpoint_url']}")
        if len(skipped) > 20:
            print(f"  ... and {len(skipped) - 20} more")

    total_missing = sum(len(v) for v in would_mark_missing_by_source.values())
    if total_missing:
        print(f"\n{total_missing} previously-imported listing(s) are no longer present in this sync and would be "
              f"marked missing (stale):")
        for source, items in would_mark_missing_by_source.items():
            for item in items[:20]:
                print(f"  [{source}] {item['id']}  {item['name']!r}")

    if not args.apply:
        print(f"\n{len(importable)} record(s) would be inserted or updated.")
        print("\nDRY RUN: nothing written. Re-run with --apply to write these changes.")
        return EXIT_OK

    sources = sorted(sources_in_sync)
    if importable or total_missing:
        confirm_phrase = sources[0] if len(sources) == 1 else "IMPORT"
        prompt = f"\nType the source ({confirm_phrase}) to confirm: " if len(sources) == 1 else (
            f"\n{len(sources)} different sources in this batch: {', '.join(sources)}. Type IMPORT to confirm: "
        )
        if not args.yes and input(prompt).strip() != confirm_phrase:
            raise AdminError("confirmation did not match; nothing written.", EXIT_GUARD)

    inserted, updated = [], []
    for row in importable:
        result, was_inserted = db.import_upsert(row)
        (inserted if was_inserted else updated).append(result)

    newly_missing: list[str] = []
    for source in sources_in_sync:
        source_rows = by_source.get(source, [])
        newly_missing += db.mark_missing_from_source(source, [r["endpoint_url"] for r in source_rows], now)

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
