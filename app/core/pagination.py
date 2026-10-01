"""Opaque, self-describing keyset cursors for GET /listings and search_listings.

Three shapes, chosen by which ordering a page is using:
  - activity: plain newest-last-activity-first browsing (no search query). Same shape
    as before relevance-ranked search existed (`{"a": ..., "i": ...}`), so a cursor
    minted before this file grew ranked search keeps working for the plain-browse path.
  - rank: a full-text search in progress, ordered by ts_rank.
  - similarity: a search whose full-text pass found nothing, now resuming the
    trigram-similarity fallback (app/core/db.py).

A rank/similarity cursor carries a short fingerprint of the query text it belongs to, so
it can never be reused - accidentally or otherwise - to resume a DIFFERENT search; and it
locks in which of the two search modes a multi-page search continues in, rather than
re-deciding per page (a search that found full-text hits on page 1 keeps using full-text
ranking through to its last page, even if, hypothetically, that would have come out
differently if decided fresh - the stable, predictable choice for someone paging through
one set of results).
"""

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Union

from app.core.errors import ApiError

FINGERPRINT_LENGTH = 16


def query_fingerprint(q: str) -> str:
    return hashlib.sha256(q.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


@dataclass(frozen=True)
class ActivityCursor:
    activity: datetime
    id: str
    mode: Literal["activity"] = "activity"


@dataclass(frozen=True)
class RankCursor:
    value: float
    id: str
    fingerprint: str
    mode: Literal["rank"] = "rank"


@dataclass(frozen=True)
class SimilarityCursor:
    value: float
    id: str
    fingerprint: str
    mode: Literal["similarity"] = "similarity"


AnyCursor = Union[ActivityCursor, RankCursor, SimilarityCursor]


def encode_activity_cursor(activity: datetime, listing_id: str) -> str:
    return _encode({"a": activity.isoformat(), "i": listing_id})


def encode_rank_cursor(value: float, listing_id: str, q: str) -> str:
    return _encode({"r": value, "i": listing_id, "qh": query_fingerprint(q)})


def encode_similarity_cursor(value: float, listing_id: str, q: str) -> str:
    return _encode({"s": value, "i": listing_id, "qh": query_fingerprint(q)})


def _encode(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _invalid(detail: str) -> ApiError:
    return ApiError(422, "invalid_cursor", detail)


def decode_cursor(cursor: str) -> AnyCursor:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        listing_id = data["i"]
        if not isinstance(listing_id, str) or not (1 <= len(listing_id) <= 64):
            raise ValueError("bad id")
        if "a" in data:
            activity = datetime.fromisoformat(data["a"])
            if activity.tzinfo is None:
                raise ValueError("naive datetime")
            return ActivityCursor(activity=activity, id=listing_id)
        if "r" in data:
            return RankCursor(value=float(data["r"]), id=listing_id, fingerprint=str(data["qh"]))
        if "s" in data:
            return SimilarityCursor(value=float(data["s"]), id=listing_id, fingerprint=str(data["qh"]))
        raise ValueError("unrecognized cursor shape")
    except (binascii.Error, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise _invalid("cursor is not a valid cursor returned by this service; restart without one.") from exc


def resolve_cursor(cursor: str | None, q: str | None) -> AnyCursor | None:
    """Decode `cursor` (if given) and check it's the right kind for this request: an
    activity cursor when there's no search query, or a rank/similarity cursor whose
    fingerprint matches this exact query when there is. Raises invalid_cursor on any
    mismatch, rather than silently resuming the wrong result set."""
    if cursor is None:
        return None
    decoded = decode_cursor(cursor)
    if q:
        if decoded.mode == "activity":
            raise _invalid("This cursor is from a plain browse, not a search; restart without a cursor.")
        if decoded.fingerprint != query_fingerprint(q):
            raise _invalid("This cursor belongs to a different search query; restart without a cursor.")
    elif decoded.mode != "activity":
        raise _invalid("This cursor is from a search; restart without a cursor.")
    return decoded
