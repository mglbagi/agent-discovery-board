"""Importing listings from a third-party directory (e.g. x402 bazaar) as `verification_profile`
or any other listing_type, without ever giving the board itself control of them.

An imported listing starts unclaimed: `submitted_by` is set to
`reserved.UNCLAIMED_IMPORT_SUBMITTED_BY`, an address no private key can ever sign for, so
no normal PATCH/DELETE/heartbeat can touch it (those all require a signature from
submitted_by). Two things can happen to it next:

  * The real owner (whoever controls payment_wallet) proves that by signing
    POST /listings/{id}/claim the same way as any other signed action (see
    app/core/wallet_auth.py), which sets submitted_by = payment_wallet and
    claimed = True - from then on it behaves exactly like any other listing, and a
    later re-sync leaves its content alone (see db.import_upsert).
  * The same pay-to owner instead signs POST /listings/{id}/remove-imported, which
    hard-deletes the listing immediately and records it in the do_not_import list
    (by source + normalized endpoint) so a later sync never recreates it.

scripts/bulk_import_listings.py is the operator tool that actually runs a sync: for each
source record it calls build_import_row() to validate it, then db.import_upsert() to
write it (skipping anything in the do_not_import list), and finally
db.mark_missing_from_source() for every previously-imported listing the sync didn't see
this time.
"""

import re
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from app.core.models import ListingCreate
from app.core.reserved import UNCLAIMED_IMPORT_SUBMITTED_BY

DEFAULT_IMPORT_LISTING_TYPE = "verification_profile"
SOURCE_MAX_LENGTH = 100
SOURCE_URL_MAX_LENGTH = 2048

# Same rule as app/core/models.py's name/description fields: no null bytes or other
# control characters (tab/newline/CR excepted).
_CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ImportRecordError(ValueError):
    """A source record failed validation; carries enough detail to report which one."""


def _validate_source(value: str) -> str:
    value = value.strip()
    if not (1 <= len(value) <= SOURCE_MAX_LENGTH) or _CONTROL_CHARS.search(value):
        raise ImportRecordError(f"source must be 1-{SOURCE_MAX_LENGTH} characters with no control characters")
    return value


def _validate_source_url(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > SOURCE_URL_MAX_LENGTH or _CONTROL_CHARS.search(value):
        raise ImportRecordError(f"source_url must be at most {SOURCE_URL_MAX_LENGTH} characters with no control characters")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ImportRecordError("source_url must be an http(s):// URL")
    return value


def build_import_row(record: dict[str, Any], *, source: str, now: datetime) -> dict[str, Any]:
    """Validates one source record and returns a full listings row ready for
    db.import_upsert(). Raises ImportRecordError (never a bare pydantic ValidationError)
    on anything invalid, so a caller can report exactly which record and why.

    `record` takes the same fields as POST /listings (name, description, listing_type
    [default verification_profile], task_categories, endpoint_url, payment_wallet,
    pricing_model, pricing_amount, payment_options, erc8004_identity,
    verification_agent_id) plus an optional source_url. submitted_by is never read from
    `record` - imports always start unclaimed.
    """
    source = _validate_source(source)
    source_url = _validate_source_url(record.get("source_url"))
    listing_fields = {
        "name": record.get("name"),
        "description": record.get("description"),
        "listing_type": record.get("listing_type", DEFAULT_IMPORT_LISTING_TYPE),
        "task_categories": record.get("task_categories"),
        "endpoint_url": record.get("endpoint_url"),
        "payment_wallet": record.get("payment_wallet"),
        "pricing_model": record.get("pricing_model"),
        "pricing_amount": record.get("pricing_amount"),
        "payment_options": record.get("payment_options", []),
        "erc8004_identity": record.get("erc8004_identity"),
        "verification_agent_id": record.get("verification_agent_id"),
        "submitted_by": UNCLAIMED_IMPORT_SUBMITTED_BY,
    }
    try:
        validated = ListingCreate(**listing_fields)
    except Exception as exc:  # pydantic ValidationError - re-raised with our own type
        raise ImportRecordError(str(exc)) from exc

    return {
        **validated.model_dump(),
        "id": str(uuid.uuid4()),
        "status": "active",
        # Never a test listing, even if a source record's name happens to start with
        # "test-" (app/core/demo_data.py's prefix is about OUR demos, not imported data).
        "is_test": False,
        "created_at": now,
        "updated_at": now,
        "claimed": False,
        "source": source,
        "source_url": source_url,
        "imported_at": now,
        "last_synced_at": now,
    }
