"""OPERATOR-ONLY: change (or, with --delete, permanently remove) one listing directly in
the database, bypassing wallet signatures. For cases the signed API cannot cover - e.g.
re-pointing submitted_by after the owner wallet is lost, or clearing out demo data -
never for routine edits (use the signed PATCH for those).

Safe by construction:
  * DRY RUN by default: prints what would change and writes nothing. Pass --apply.
  * exactly one row: the id must exist (it is the primary key) and the UPDATE is
    verified to have touched exactly one row, otherwise it is rolled back.
  * expected current values: you must state (--expect field=value, at least one) what
    you believe the listing currently contains; if anything differs nothing is written.
  * validated: the resulting listing must pass the same validation as POST /listings.
  * audited: prints before/after and appends one JSON line per applied change to the
    log file (default ./admin_changes.log, which .gitignore already excludes).
  * with --apply you must retype the listing id to confirm (skip with --yes).
  * --delete is a HARD delete of one row: only for an INACTIVE listing (deactivate an
    active one with the signed DELETE first), only with the same expected-value guards,
    and the whole deleted row is written to the audit log so it can be reconstructed.

It connects to whatever DATABASE_URL points at (the production database when run with
production settings) and prints the target host, never the credentials.

Examples:

  # See what re-pointing the owner would do (nothing is written):
  python scripts/admin_update_listing.py --id <uuid> \\
      --expect submitted_by=0xOLD... --set submitted_by=0xNEW...

  # Actually do it:
  python scripts/admin_update_listing.py --id <uuid> \\
      --expect submitted_by=0xOLD... --set submitted_by=0xNEW... --apply

  # Permanently remove an inactive demo listing (dry run; add --apply to do it):
  python scripts/admin_update_listing.py --id <uuid> --expect status=inactive \\
      --expect name="the exact name" --delete

  # Structured fields take JSON:
  python scripts/admin_update_listing.py --id <uuid> --expect status=active \\
      --set payment_options='[{"network":"eip155:8453","asset":"0x...","pay_to":"0x..."}]'
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

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import app  # noqa: E402,F401  (loads .env)
from app.core.endpoint import normalize_endpoint_url  # noqa: E402
from app.core.models import ListingCreate  # noqa: E402
from app.core.reserved import is_reserved_address  # noqa: E402

EDITABLE_FIELDS = (
    "name", "description", "listing_type", "task_categories", "endpoint_url", "payment_wallet",
    "pricing_model", "pricing_amount", "payment_options", "erc8004_identity", "verification_agent_id",
    "submitted_by", "status",
)
NULLABLE_FIELDS = ("pricing_model", "pricing_amount", "erc8004_identity", "verification_agent_id")
JSON_FIELDS = ("task_categories", "payment_options")
COLUMNS = ", ".join(("id",) + EDITABLE_FIELDS + ("created_at", "updated_at", "last_seen_at"))

EXIT_OK, EXIT_USAGE, EXIT_GUARD = 0, 1, 2


class AdminError(Exception):
    def __init__(self, message: str, code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.code = code


def _parse_pairs(pairs: list[str], flag: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        field, sep, raw = pair.partition("=")
        if not sep or field not in EDITABLE_FIELDS:
            raise AdminError(f"{flag} {pair!r}: expected field=value with field one of {list(EDITABLE_FIELDS)}")
        if field in out:
            raise AdminError(f"{flag} {field} given more than once")
        if field in JSON_FIELDS:
            try:
                out[field] = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AdminError(f"{flag} {field}: value must be JSON ({exc})") from exc
        else:
            out[field] = raw
    return out


def _target(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.hostname}{parsed.path}"


def _fetch(conn: psycopg.Connection, listing_id: str) -> list[dict[str, Any]]:
    return conn.execute(f"SELECT {COLUMNS} FROM listings WHERE id = %s", (listing_id,)).fetchall()


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _check_expected(before: dict[str, Any], expected: dict[str, Any]) -> None:
    mismatches = {f: (want, before[f]) for f, want in expected.items() if before[f] != want}
    if mismatches:
        lines = "\n".join(f"  {f}: expected {w!r}, actual {a!r}" for f, (w, a) in mismatches.items())
        raise AdminError(f"the listing does not have the expected current values; nothing written:\n{lines}", EXIT_GUARD)


def _confirm(args: argparse.Namespace) -> None:
    if not args.yes and input(f"\nType the listing id ({args.id}) to confirm: ").strip() != args.id:
        raise AdminError("confirmation did not match; nothing written.", EXIT_GUARD)


def _append_log(args: argparse.Namespace, entry: dict[str, Any]) -> None:
    with open(args.log_file, "a", encoding="utf-8") as log:
        log.write(json.dumps(entry, default=str, sort_keys=True) + "\n")
    print(f"Logged to {args.log_file}")


def _guard_sql(expected: dict[str, Any], params: dict[str, Any]) -> str:
    for f in expected:
        params[f"_exp_{f}"] = expected[f]
    return " AND ".join(f"{f} IS NOT DISTINCT FROM %(_exp_{f})s" for f in expected)


def _run_delete(args: argparse.Namespace, url: str, expected: dict[str, Any]) -> int:
    print(f"Target database: {_target(url)}")
    print(f"Mode: HARD DELETE {'(APPLY)' if args.apply else '(DRY RUN, nothing will be written)'}")

    with psycopg.connect(url, row_factory=dict_row) as conn:
        rows = _fetch(conn, args.id)
        if len(rows) != 1:
            raise AdminError(f"expected exactly one listing with id {args.id!r}, found {len(rows)}.", EXIT_GUARD)
        before = rows[0]
        _check_expected(before, expected)
        if before["status"] != "inactive":
            raise AdminError(
                "refusing to hard-delete a listing that is not inactive; deactivate it first "
                "(the signed DELETE, or --set status=inactive).",
                EXIT_GUARD,
            )

        print(f"\nWould permanently delete listing {args.id}:\n{_json({f: before[f] for f in ('id', 'name', 'listing_type', 'status', 'endpoint_url', 'submitted_by', 'created_at', 'updated_at')})}")
        if not args.apply:
            print("\nDRY RUN: nothing deleted. Re-run with --apply to delete it.")
            return EXIT_OK
        _confirm(args)

        params: dict[str, Any] = {"_id": args.id}
        guard = _guard_sql(expected, params)
        with conn.transaction():
            count = conn.execute(
                f"DELETE FROM listings WHERE id = %(_id)s AND status = 'inactive' AND {guard}", params
            ).rowcount
            if count != 1:
                raise AdminError(f"the DELETE touched {count} rows, expected exactly 1; rolled back.", EXIT_GUARD)
        if _fetch(conn, args.id):
            raise AdminError("the row is still present after the DELETE.", EXIT_GUARD)

    print(f"\nDeleted listing {args.id}.")
    _append_log(
        args,
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "operator": getpass.getuser(),
            "db": _target(url),
            "listing_id": args.id,
            "action": "hard-delete",
            "deleted_row": before,
        },
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--id", required=True, help="the listing's id")
    ap.add_argument("--expect", action="append", default=[], metavar="FIELD=VALUE",
                    help="current value the listing must have (repeatable; at least one required)")
    ap.add_argument("--set", dest="sets", action="append", default=[], metavar="FIELD=VALUE",
                    help="new value (repeatable; task_categories and payment_options take JSON)")
    ap.add_argument("--unset", action="append", default=[], metavar="FIELD",
                    help=f"set a nullable field to NULL ({', '.join(NULLABLE_FIELDS)})")
    ap.add_argument("--delete", action="store_true",
                    help="HARD-delete the listing (inactive listings only); cannot be combined with --set/--unset")
    ap.add_argument("--apply", action="store_true", help="write the change (default is a dry run)")
    ap.add_argument("--yes", action="store_true", help="with --apply, skip the retype-the-id confirmation")
    ap.add_argument("--log-file", default="admin_changes.log", help="audit log, one JSON line per applied change")
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
    expected = _parse_pairs(args.expect, "--expect")
    if not expected:
        raise AdminError("at least one --expect FIELD=VALUE is required (state what you believe is there now).")
    if args.delete:
        if args.sets or args.unset:
            raise AdminError("--delete cannot be combined with --set/--unset.")
        return _run_delete(args, url, expected)
    changes = _parse_pairs(args.sets, "--set")
    for field in args.unset:
        if field not in NULLABLE_FIELDS:
            raise AdminError(f"--unset {field}: only nullable fields can be unset: {list(NULLABLE_FIELDS)}")
        if field in changes:
            raise AdminError(f"{field} is both --set and --unset")
        changes[field] = None
    if not changes:
        raise AdminError("nothing to do: give at least one --set or --unset.")

    print(f"Target database: {_target(url)}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN (nothing will be written)'}")

    with psycopg.connect(url, row_factory=dict_row) as conn:
        rows = _fetch(conn, args.id)
        if len(rows) != 1:
            raise AdminError(f"expected exactly one listing with id {args.id!r}, found {len(rows)}.", EXIT_GUARD)
        before = rows[0]

        _check_expected(before, expected)

        candidate = {**before, **changes}
        try:
            validated = ListingCreate(**{k: candidate[k] for k in ListingCreate.model_fields}).model_dump()
        except ValidationError as exc:
            raise AdminError(f"the resulting listing is invalid:\n{exc}") from exc
        if candidate["status"] not in ("active", "inactive"):
            raise AdminError("status must be 'active' or 'inactive'.")
        if is_reserved_address(candidate["submitted_by"]):
            raise AdminError("reserved_address: that submitted_by has a publicly known private key; refusing.")

        final = {f: (candidate["status"] if f == "status" else validated[f]) for f in EDITABLE_FIELDS}
        diff = {f: {"before": before[f], "after": final[f]} for f in changes if final[f] != before[f]}
        if not diff:
            print("The listing already has these values; nothing to change.")
            return EXIT_OK

        print(f"\nListing {args.id}\n\nBEFORE (changed fields):\n{_json({f: d['before'] for f, d in diff.items()})}")
        print(f"\nAFTER (changed fields):\n{_json({f: d['after'] for f, d in diff.items()})}")

        if not args.apply:
            print("\nDRY RUN: no changes written. Re-run with --apply to write them.")
            return EXIT_OK

        _confirm(args)

        now = datetime.now(timezone.utc)
        assignments = {f: final[f] for f in diff}
        if "endpoint_url" in assignments:
            assignments["endpoint_key"] = normalize_endpoint_url(final["endpoint_url"])
        params: dict[str, Any] = {
            **{f: (Jsonb(v) if f == "payment_options" else v) for f, v in assignments.items()},
            "updated_at": now,
            "_id": args.id,
        }
        guard = _guard_sql(expected, params)
        set_clause = ", ".join(f"{f} = %({f})s" for f in list(assignments) + ["updated_at"])
        try:
            with conn.transaction():
                count = conn.execute(
                    f"UPDATE listings SET {set_clause} WHERE id = %(_id)s AND {guard}", params
                ).rowcount
                if count != 1:
                    raise AdminError(f"the UPDATE touched {count} rows, expected exactly 1; rolled back.", EXIT_GUARD)
        except psycopg.errors.UniqueViolation as exc:
            raise AdminError(
                "rejected: another ACTIVE offering already has this endpoint_url and submitted_by "
                f"(unique index). Nothing written. ({exc.diag.constraint_name})",
                EXIT_GUARD,
            ) from exc

        after = _fetch(conn, args.id)[0]

    print(f"\nApplied. Listing after the change:\n{_json(after)}")
    entry = {
        "ts": now.isoformat(),
        "operator": getpass.getuser(),
        "db": _target(url),
        "listing_id": args.id,
        "changes": diff,
    }
    _append_log(args, entry)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
