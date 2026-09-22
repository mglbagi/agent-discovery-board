"""Deterministic bytes for a JSON-ish dict, used only by wallet_auth.py to hash a
PATCH body into the message that gets signed.

This service's signed payloads (listing fields) never contain floats — prices are
free-text strings (e.g. "$0.02", "$10/month"), not numbers — so plain sort_keys JSON
is sufficient here. (Contrast the verification service, whose signed responses do
contain floats and so need RFC 8785's specific number formatting; that complexity
doesn't apply to this service's data and isn't reproduced here.)
"""

import json
from typing import Any


def canonical_json(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
