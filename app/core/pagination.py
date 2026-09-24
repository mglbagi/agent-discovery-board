"""Opaque keyset cursors for GET /listings and search_listings.

A cursor encodes the (last_activity_at, id) of the last item on the previous page, so
the next page is "everything strictly after that point" in the (last_activity_at DESC,
id DESC) order - no skipped or repeated items when listings are added meanwhile
(unlike offset paging, which is kept only for backward compatibility).
"""

import base64
import binascii
import json
from datetime import datetime

from app.core.errors import ApiError


def encode_cursor(activity: datetime, listing_id: str) -> str:
    raw = json.dumps({"a": activity.isoformat(), "i": listing_id}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        activity = datetime.fromisoformat(data["a"])
        listing_id = data["i"]
        if activity.tzinfo is None or not isinstance(listing_id, str) or not (1 <= len(listing_id) <= 64):
            raise ValueError("bad cursor contents")
        return activity, listing_id
    except (binascii.Error, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ApiError(
            422, "invalid_cursor", "cursor is not a valid cursor returned by this service; restart without one."
        ) from exc
