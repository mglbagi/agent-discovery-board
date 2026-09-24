"""endpoint_url normalization, used to detect duplicate listings.

Two URLs that differ only in scheme/host case, an explicit default port, a fragment,
duplicate or trailing slashes in the path, query-parameter order, or userinfo point at
the same service and normalize to the same key. Path case and query values are kept
as-is (they can be significant).
"""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit


def normalize_endpoint_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = parts.port
    default_port = {"https": 443, "http": 80}.get(scheme)
    netloc = host if port is None or port == default_port else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return f"{scheme}://{netloc}{path}" + (f"?{query}" if query else "")
