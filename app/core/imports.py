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
    later re-sync leaves its content alone (see db.import_upsert). payment_wallet may be
    an EVM or a Solana address (app/core/models.py's _validate_payment_wallet); claiming
    is EVM-only for now (EIP-191 has no Solana/ed25519 equivalent here), so a
    Solana-only listing stays unclaimed until that's built (see the README).
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

from pydantic import ValidationError

from app.core.models import ListingCreate, PaymentOption, validate_generic_url
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
    if len(value) > SOURCE_URL_MAX_LENGTH:
        raise ImportRecordError(f"source_url must be at most {SOURCE_URL_MAX_LENGTH} characters")
    try:
        return validate_generic_url(value, "source_url", SOURCE_URL_MAX_LENGTH)
    except ValueError as exc:
        raise ImportRecordError(str(exc)) from exc


def _resolve_source(record: dict[str, Any], source: str | None) -> str:
    """`source` (the script's --source, when given) always wins; otherwise a record must
    carry its own - either record["_import"]["source"] (the sales-team export's own
    shape) or a flat record["source"]."""
    if source is not None:
        return _validate_source(source)
    own = (record.get("_import") or {}).get("source") or record.get("source")
    if not own:
        raise ImportRecordError(
            "no source: pass --source, or include _import.source (or source) in the record"
        )
    return _validate_source(own)


def _resolve_template_url(record: dict[str, Any]) -> str | None:
    """A flat record["template_url"] (our own canonical shape) wins; otherwise falls back
    to record["template"]["url"] (the sales-team export's nested shape - template_id and
    label aren't part of our schema and are dropped)."""
    flat = record.get("template_url")
    if flat is not None:
        return flat
    return (record.get("template") or {}).get("url")


def _filter_payment_options(payment_options: list[Any]) -> tuple[list[dict[str, Any]], int]:
    """Keeps only entries this board can actually represent (eip155/solana, well-formed
    address/amount - see PaymentOption), dropping the rest rather than failing the whole
    record over one payment network we don't support (xrpl, algorand, stellar, a bare
    "base"/"solana" with no chain reference, etc. - all seen in real source data).
    Returns (kept, dropped_count)."""
    kept = []
    for option in payment_options or []:
        try:
            PaymentOption(**option)
        except (ValidationError, TypeError):
            continue
        kept.append(option)
    return kept, len(payment_options or []) - len(kept)


def _resolve_payment_wallet(record: dict[str, Any], payment_options: list[dict[str, Any]]) -> str | None:
    """The record's own payment_wallet wins; otherwise falls back to an EVM
    payment_option's pay_to, then a Solana one's - a seller with only a Solana pay-to
    still gets a (currently unclaimable) listing rather than being dropped for lacking
    the historically-EVM-only payment_wallet field."""
    direct = record.get("payment_wallet")
    if direct is not None:
        return direct
    evm = next((o for o in payment_options if o["network"].startswith("eip155:")), None)
    solana = next((o for o in payment_options if o["network"].startswith("solana:")), None)
    return (evm or solana or {}).get("pay_to")


def build_import_row(record: dict[str, Any], *, source: str | None = None, now: datetime) -> dict[str, Any]:
    """Validates one source record and returns a full listings row ready for
    db.import_upsert(). Raises ImportRecordError (never a bare pydantic ValidationError)
    on anything invalid, so a caller can report exactly which record and why.

    `record` takes the same fields as POST /listings (name, description, listing_type
    [default verification_profile], task_categories, endpoint_url, payment_wallet,
    pricing_model, pricing_amount, payment_options, erc8004_identity,
    verification_agent_id, output_schema, verification, template_url) plus
    source/source_url. submitted_by is never read from `record` - imports always start
    unclaimed. `source`, if given, overrides any source/_import.source in the record;
    see _resolve_source.

    payment_options entries this board can't represent (an unsupported chain, or a
    malformed address/amount) are silently dropped rather than failing the record - see
    _filter_payment_options. If the record has no payment_wallet of its own, one is
    derived from a surviving EVM (preferred) or Solana payment_option's pay_to - see
    _resolve_payment_wallet. A record left with neither is rejected: payment_wallet is
    required.
    """
    resolved_source = _resolve_source(record, source)
    source_url = _validate_source_url(record.get("source_url"))
    payment_options, _ = _filter_payment_options(record.get("payment_options") or [])
    payment_wallet = _resolve_payment_wallet(record, payment_options)
    if payment_wallet is None:
        raise ImportRecordError(
            "no usable payment_wallet: the record has none, and none of its payment_options are on a "
            "supported network (eip155 or solana) with a well-formed pay_to to fall back to"
        )
    listing_fields = {
        "name": record.get("name"),
        "description": record.get("description"),
        "listing_type": record.get("listing_type", DEFAULT_IMPORT_LISTING_TYPE),
        "task_categories": record.get("task_categories"),
        "endpoint_url": record.get("endpoint_url"),
        "payment_wallet": payment_wallet,
        "pricing_model": record.get("pricing_model"),
        "pricing_amount": record.get("pricing_amount"),
        "payment_options": payment_options,
        "erc8004_identity": record.get("erc8004_identity"),
        "verification_agent_id": record.get("verification_agent_id"),
        "output_schema": record.get("output_schema"),
        "verification": record.get("verification"),
        "template_url": _resolve_template_url(record),
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
        "source": resolved_source,
        "source_url": source_url,
        "imported_at": now,
        "last_synced_at": now,
    }
