"""One sync, two front ends: scripts/bulk_import_listings.py (the operator's command line)
and POST /admin/import (the protected endpoint a publisher calls) both run exactly this -
the same parsing, the same per-record validation (app/core/imports.py's build_import_row),
the same do-not-import check, the same upsert by (source, endpoint) that never touches a
claimed listing's content (db.import_upsert), and the same "previously imported, absent
from this batch" -> stale marking (db.mark_missing_from_source). Keeping it in one place
is what makes "the endpoint behaves like the script" a fact rather than an intention.

A sync is a FULL listing of a source: whatever the source previously supplied and this
batch no longer contains is marked stale (flagged, never deleted). Records with a
payment problem or any other invalid field are rejected individually and reported; they
never abort the rest of the batch.
"""

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core import db
from app.core.imports import ImportRecordError, build_import_row


class ImportInputError(ValueError):
    """The batch as a whole is unusable (not JSON / not records) - as opposed to one bad
    record, which is just rejected and counted."""


def parse_records(text: str, label: str = "the batch") -> list[Any]:
    """A JSON array of records, or one JSON object per non-blank line (JSONL)."""
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ImportInputError(f"could not parse {label} as a JSON array: {exc}") from exc
        if not isinstance(data, list):
            raise ImportInputError(f"{label} must contain a JSON array of records.")
        return data
    records: list[Any] = []
    for i, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ImportInputError(f"{label}, line {i + 1}: not valid JSON: {exc}") from exc
    return records


def board_template_base() -> str | None:
    """The board's own public URL (SERVICE_BASE_URL), which imported listings' template_url
    is built from. None only where it isn't configured (e.g. an operator's shell)."""
    value = (os.getenv("SERVICE_BASE_URL") or "").strip().rstrip("/")
    return value or None


def validate_all(
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


@dataclass
class SyncPlan:
    rows: list[dict[str, Any]]  # every valid record
    rejected: list[dict[str, Any]]
    stats: dict[str, int]
    importable: list[dict[str, Any]]  # valid, not on do-not-import, one per (source, endpoint), last one wins
    skipped: list[dict[str, Any]]  # valid but on the do-not-import list
    duplicates_in_batch: int  # extra copies of an endpoint inside the batch (collapsed into one write)
    by_source: dict[str, list[dict[str, Any]]]
    sources_in_sync: set[str]
    would_mark_missing: dict[str, list[dict[str, Any]]]  # source -> [{id, name}]
    mark_missing: bool = True

    @property
    def total_missing(self) -> int:
        return sum(len(v) for v in self.would_mark_missing.values())


def plan_sync(
    records: list[Any],
    source: str | None,
    now: datetime,
    *,
    allow_empty_sync: bool = True,
    mark_missing: bool = True,
) -> SyncPlan:
    """Validates and plans a sync; reads the database, writes nothing.

    `allow_empty_sync`: the script treats an explicit --source with no valid records (an
    empty file, or every record rejected) as a real, empty sync, so everything previously
    imported for it is reported/marked missing. An unattended caller must not: a truncated
    upload or an upstream formatting bug would silently stale a whole source. With
    allow_empty_sync=False a source takes part in the sync only if the batch has at least
    one valid record for it.
    `mark_missing=False` skips the stale marking altogether (a partial/delta batch)."""
    rows, rejected, stats = validate_all(records, source, now)

    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_source[row["source"]].append(row)
    sources_in_sync = set(by_source) | ({source} if (source and allow_empty_sync) else set())

    skipped: list[dict[str, Any]] = []
    importable: list[dict[str, Any]] = []
    would_mark_missing: dict[str, list[dict[str, Any]]] = {}
    duplicates = 0
    for src in sorted(sources_in_sync):
        source_rows = by_source.get(src, [])
        urls = [r["endpoint_url"] for r in source_rows]
        blocked = db.do_not_import_keys(src, urls)
        latest: dict[str, dict[str, Any]] = {}
        for row in source_rows:
            key = db.normalize_endpoint_url(row["endpoint_url"])
            if key in blocked:
                skipped.append(row)
                continue
            if key in latest:
                duplicates += 1
            latest[key] = row  # the same end state as upserting each in turn
        importable.extend(latest.values())
        would_mark_missing[src] = db.preview_missing_from_source(src, urls) if mark_missing else []

    return SyncPlan(
        rows=rows, rejected=rejected, stats=stats, importable=importable, skipped=skipped,
        duplicates_in_batch=duplicates, by_source=dict(by_source), sources_in_sync=sources_in_sync,
        would_mark_missing=would_mark_missing, mark_missing=mark_missing,
    )


def dry_run_counts(plan: SyncPlan) -> dict[str, int]:
    """What apply_plan would do, as counts: added vs updated (a read-only look at what
    already exists), claimed listings among the updated (only their sync timestamps move),
    and how many would be marked stale."""
    existing: dict[str, bool] = {}
    for src in plan.sources_in_sync:
        urls = [r["endpoint_url"] for r in plan.importable if r["source"] == src]
        existing.update({(src, k): v for k, v in db.existing_import_listings(src, urls).items()})
    added = updated = claimed = 0
    for row in plan.importable:
        key = (row["source"], db.normalize_endpoint_url(row["endpoint_url"]))
        if key in existing:
            updated += 1
            claimed += bool(existing[key])
        else:
            added += 1
    return {"added": added, "updated": updated, "claimed_content_preserved": claimed, "stale": plan.total_missing}


@dataclass
class SyncOutcome:
    inserted: list[dict[str, Any]] = field(default_factory=list)
    updated: list[dict[str, Any]] = field(default_factory=list)
    newly_missing: list[str] = field(default_factory=list)


def apply_plan(plan: SyncPlan, now: datetime, *, template_base: str | None) -> SyncOutcome:
    """Writes the plan: every importable row (upsert), then - only after all of those
    succeeded - the stale marking, so a failure part-way never stales anything. Safe to
    re-run: upserts are idempotent and rows already marked are left alone."""
    outcome = SyncOutcome()
    for row in plan.importable:
        result, was_inserted = db.import_upsert(row, template_base=template_base)
        (outcome.inserted if was_inserted else outcome.updated).append(result)
    if plan.mark_missing:
        for src in sorted(plan.sources_in_sync):
            seen = [r["endpoint_url"] for r in plan.by_source.get(src, [])]
            outcome.newly_missing += db.mark_missing_from_source(src, seen, now)
    return outcome
