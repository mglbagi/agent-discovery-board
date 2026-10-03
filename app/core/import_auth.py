"""Authorization, limits and the audit trail for POST /admin/import.

The endpoint is for exactly one caller - whoever publishes the source directory - and is
authorized by exactly one thing: the IMPORT_API_KEY environment variable. No account, no
wallet signature, no second mechanism. Rules:

  * IMPORT_API_KEY is required. This module raises at import, i.e. at startup, if it is
    missing or empty (after stripping surrounding whitespace, which a pasted secret often
    carries), so a deploy without it fails loudly instead of running with the endpoint
    open or silently dead. The key is never logged, echoed or put in an error message.
  * The caller sends it as `Authorization: Bearer <key>`. It is compared in constant time
    (SHA-256 of both sides, then hmac.compare_digest, so neither the content nor the length
    of the real key shows up in response timing). A missing or wrong key gets the same 401.
  * Wrong-key attempts have their own, tighter rate limit, so the endpoint can't be used to
    guess the key; a correct key is not subject to it (an attacker hammering from a shared
    address can't lock the real publisher out) but is still subject to the normal limit.
  * Every call - accepted, refused or failed - writes exactly one audit line (below), made
    of counts and outcomes only: never the key, never any record content.
"""

import hashlib
import hmac
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from app.core.rate_limit import RateLimiter

IMPORT_PATH = "/admin/import"


def _load_key() -> str:
    key = (os.getenv("IMPORT_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "IMPORT_API_KEY is not set (or is empty). It authorizes POST /admin/import and is required; set it "
            "to a long random secret in the service's environment."
        )
    return key


_KEY_DIGEST = hashlib.sha256(_load_key().encode("utf-8")).digest()

# One batch of listings per request: the whole source, as JSONL. The full x402 bazaar file
# is about 8.5 MB today; this leaves room to grow without letting a single request hold
# unbounded memory (the body is read, parsed and validated in memory).
IMPORT_MAX_BODY_BYTES = int(os.getenv("IMPORT_MAX_BODY_BYTES", str(16 * 1024 * 1024)))


def key_matches(supplied: str | None) -> bool:
    if not supplied:
        return False
    digest = hashlib.sha256(supplied.strip().encode("utf-8")).digest()
    return hmac.compare_digest(digest, _KEY_DIGEST)


def bearer_token(authorization_header: str | None) -> str | None:
    """The token of `Authorization: Bearer <token>`, or None for anything else."""
    if not authorization_header:
        return None
    scheme, _, token = authorization_header.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


# A real publisher calls this a few times a day. Both limits are per client address, with a
# global daily cap behind them (see app/core/rate_limit.py for how the two layers work).
import_limiter = RateLimiter(
    max_requests=int(os.getenv("IMPORT_RATE_LIMIT_MAX_REQUESTS", "6")),
    window_seconds=float(os.getenv("IMPORT_RATE_LIMIT_WINDOW_SECONDS", "60")),
    global_daily_cap=int(os.getenv("IMPORT_GLOBAL_DAILY_CAP", "200")),
)
import_auth_failure_limiter = RateLimiter(
    max_requests=int(os.getenv("IMPORT_AUTH_FAILURE_RATE_LIMIT_MAX_REQUESTS", "10")),
    window_seconds=float(os.getenv("IMPORT_AUTH_FAILURE_RATE_LIMIT_WINDOW_SECONDS", "60")),
    global_daily_cap=int(os.getenv("IMPORT_AUTH_FAILURE_GLOBAL_DAILY_CAP", "5000")),
)

audit_logger = logging.getLogger("import.audit")
audit_logger.setLevel(logging.INFO)
if not audit_logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger.addHandler(_handler)
    audit_logger.propagate = False

# Only these keys can ever appear in an audit line. Anything else passed to audit() is
# dropped, so a future edit can't accidentally log a header or a record.
_AUDIT_FIELDS = (
    "outcome", "status", "dry_run", "source", "client", "bytes", "records", "valid", "added", "updated",
    "stale", "rejected", "skipped_do_not_import", "claimed_content_preserved", "mark_missing", "duration_ms",
)
_SOURCE_LOG_MAX = 100


def audit(**fields: Any) -> None:
    """One line per call: `[import-audit] {json}` with counts and outcomes only."""
    entry: dict[str, Any] = {"event": "admin_import", "ts": datetime.now(timezone.utc).isoformat()}
    for name in _AUDIT_FIELDS:
        if name in fields and fields[name] is not None:
            value = fields[name]
            if name == "source":
                value = str(value)[:_SOURCE_LOG_MAX]
            entry[name] = value
    audit_logger.info("[import-audit] " + json.dumps(entry, sort_keys=True, default=str))
