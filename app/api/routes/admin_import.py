"""POST /admin/import - the same sync as scripts/bulk_import_listings.py, callable by the
publisher of a source directory. Authorization, limits and the audit line are in
app/core/import_auth.py; the sync itself (validation, upsert by source + endpoint, never
touching a claimed listing, do-not-import, stale marking) is app/core/import_sync.py, shared
with the script, so the two cannot drift apart.

    curl -X POST "$BOARD/admin/import?dry_run=false" \\
         -H "Authorization: Bearer $IMPORT_API_KEY" \\
         -H "Content-Type: application/x-ndjson" --data-binary @listings-import.jsonl

Query parameters (read only after the key is accepted):
  dry_run        default true - counts only, nothing written. Pass dry_run=false to write.
  source         optional; forces one source for every record (like the script's --source).
  mark_missing   default true - listings this source previously supplied and the batch no
                 longer contains are marked stale (flagged, never deleted). false skips that,
                 for a partial batch.

A batch with no valid records never marks anything stale, whatever `source` says: an empty or
fully-rejected upload is a broken upload, not a statement that the source has no listings.
"""

import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core import import_sync
from app.core.errors import ApiError
from app.core.import_auth import (
    IMPORT_MAX_BODY_BYTES,
    IMPORT_PATH,
    audit,
    bearer_token,
    import_auth_failure_limiter,
    import_limiter,
    key_matches,
)

router = APIRouter()

_MAX_REJECTED_DETAILS = 50
_import_running = threading.Lock()

_OUTCOME_BY_STATUS = {
    401: "unauthorized", 409: "busy", 413: "too_large", 422: "bad_batch", 429: "rate_limited",
}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _bool_param(request: Request, name: str, default: bool) -> bool:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    raise ApiError(422, "validation_error", f"{name} must be true or false")


def _too_large() -> ApiError:
    return ApiError(
        413, "body_too_large", f"The batch exceeds the {IMPORT_MAX_BODY_BYTES} byte limit; split it into several requests."
    )


async def _read_body(request: Request, info: dict[str, Any]) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > IMPORT_MAX_BODY_BYTES:
        raise _too_large()
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > IMPORT_MAX_BODY_BYTES:
            raise _too_large()
        chunks.append(chunk)
    info["bytes"] = total
    return b"".join(chunks)


async def _handle(request: Request, info: dict[str, Any]) -> dict[str, Any]:
    # 1. Authorization first - before any parameter, body or database work. Wrong-key
    #    attempts are counted against their own tight limit so the key can't be guessed.
    if not key_matches(bearer_token(request.headers.get("authorization"))):
        import_auth_failure_limiter.check(request)
        raise ApiError(
            401, "unauthorized", "A valid import key is required: Authorization: Bearer <key>.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    import_limiter.check(request)

    dry_run = _bool_param(request, "dry_run", True)
    mark_missing = _bool_param(request, "mark_missing", True)
    source = (request.query_params.get("source") or "").strip() or None
    info.update(dry_run=dry_run, mark_missing=mark_missing, source=source)

    if not _import_running.acquire(blocking=False):
        raise ApiError(409, "conflict", "An import is already running; retry in a minute.")
    try:
        body = await _read_body(request, info)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApiError(422, "validation_error", f"The batch must be UTF-8 text: {exc}") from exc
        try:
            records = import_sync.parse_records(text, "the batch")
        except import_sync.ImportInputError as exc:
            raise ApiError(422, "validation_error", str(exc)) from exc
        if not records:
            raise ApiError(422, "validation_error", "The batch has no records.")
        info["records"] = len(records)

        now = datetime.now(timezone.utc)
        plan = await asyncio.to_thread(
            import_sync.plan_sync, records, source, now, allow_empty_sync=False, mark_missing=mark_missing
        )
        counts = await asyncio.to_thread(import_sync.dry_run_counts, plan)
        if dry_run:
            added, updated, stale = counts["added"], counts["updated"], counts["stale"]
        else:
            outcome = await asyncio.to_thread(
                import_sync.apply_plan, plan, now, template_base=import_sync.board_template_base()
            )
            added, updated, stale = len(outcome.inserted), len(outcome.updated), len(outcome.newly_missing)

        result = {
            "dry_run": dry_run,
            "applied": not dry_run,
            "sources": sorted(plan.sources_in_sync),
            "records": len(records),
            "valid": len(plan.rows),
            "added": added,
            "updated": updated,
            "stale": stale,
            "rejected": len(plan.rejected),
            "skipped_do_not_import": len(plan.skipped),
            "claimed_content_preserved": counts["claimed_content_preserved"],
            "duplicates_in_batch": plan.duplicates_in_batch,
            "mark_missing": mark_missing,
            "rejected_details": plan.rejected[:_MAX_REJECTED_DETAILS],
            "rejected_details_omitted": max(0, len(plan.rejected) - _MAX_REJECTED_DETAILS),
        }
        info.update(
            valid=result["valid"], added=added, updated=updated, stale=stale, rejected=result["rejected"],
            skipped_do_not_import=result["skipped_do_not_import"],
            claimed_content_preserved=result["claimed_content_preserved"],
        )
        return result
    finally:
        _import_running.release()


@router.post(IMPORT_PATH, include_in_schema=False)
async def import_listings(request: Request) -> JSONResponse:
    started = time.monotonic()
    info: dict[str, Any] = {"client": _client_ip(request)}

    def duration() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        result = await _handle(request, info)
    except HTTPException as exc:
        # Unauthenticated callers' parameters are never logged (they were never read).
        audit(**info, outcome=_OUTCOME_BY_STATUS.get(exc.status_code, "refused"), status=exc.status_code,
              duration_ms=duration())
        raise
    except Exception:
        audit(**info, outcome="error", status=500, duration_ms=duration())
        raise
    audit(**info, outcome="dry_run" if result["dry_run"] else "applied", status=200, duration_ms=duration())
    return JSONResponse(result)
