"""What the sibling verification service says about itself - its price, networks, protocol and
free path - read from its own public documents, never hard-coded here, so a listing's
`verify_output` next_action can tell an agent what a check costs and how to try one free.

Sources (both public, both fetched over plain GET):
  * {VERIFICATION_SERVICE_URL}/.well-known/x402       -> the /verify/schema resource: price,
    currency, payment networks, x402 version.
  * {VERIFICATION_SERVICE_URL}/.well-known/agent-card.json -> its MCP extension: endpoint, the
    verify_schema tool, and the free allowance (access.freeTrial).

Behavior, in order of priority:
  * A listing response never waits on the network and never fails because of this module.
    snapshot() only reads memory; if a refresh is due it starts one in a background thread
    (stale-while-revalidate).
  * Refresh every VERIFIER_INFO_TTL_SECONDS (default 900); after a failed attempt, retry no
    sooner than FAILURE_BACKOFF_SECONDS so a verifier outage is not hammered.
  * Last-known values are kept across failures AND across restarts (persisted in the board's own
    database, db.save_verifier_info), and each section is marked live (fetched successfully by
    this process, with no failure since) or last_known - see snapshot()["status"].
  * Nothing is invented: before any document has ever been read, the values are simply absent
    (status "unavailable") and the next_action says so.

VERIFIER_INFO_REFRESH=0 disables the background network fetch (the test suite sets it so no
test ever touches the real service); everything else works the same.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core import db
from app.core.score_client import VERIFICATION_SERVICE_URL

logger = logging.getLogger("app.verifier_info")

X402_URL = f"{VERIFICATION_SERVICE_URL}/.well-known/x402"
AGENT_CARD_URL = f"{VERIFICATION_SERVICE_URL}/.well-known/agent-card.json"
SOURCES = [X402_URL, AGENT_CARD_URL]

VERIFY_PATH_SUFFIX = "/verify/schema"
VERIFY_TOOL = "verify_schema"

TTL_SECONDS = float(os.getenv("VERIFIER_INFO_TTL_SECONDS", "900"))
FAILURE_BACKOFF_SECONDS = 60.0
FETCH_TIMEOUT_SECONDS = 5.0


def _refresh_enabled() -> bool:
    return os.getenv("VERIFIER_INFO_REFRESH", "1") != "0"


_lock = threading.Lock()
# section name -> {"data": {...}, "fetched_at": iso str, "live": bool}
_sections: dict[str, dict[str, Any]] = {}
_loaded_from_db = False
_refreshing = False
_last_attempt: float | None = None  # time.monotonic() of the last refresh attempt
_last_attempt_ok = True


def _fetch_json(url: str) -> dict[str, Any]:
    response = httpx.get(url, timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True)
    response.raise_for_status()
    return response.json()


# ---- parsing (pure) -------------------------------------------------------------------------------


def parse_payment(doc: dict[str, Any]) -> dict[str, Any]:
    """The /verify/schema resource out of /.well-known/x402. Raises ValueError if it is absent."""
    resource = next(
        (r for r in doc.get("resources", []) if str(r.get("url", "")).rstrip("/").endswith(VERIFY_PATH_SUFFIX)),
        None,
    )
    if resource is None:
        raise ValueError("no /verify/schema resource in the x402 document")
    networks = [o["network"] for o in resource.get("paymentOptions", []) if o.get("network")]
    if not resource.get("price") or not networks:
        raise ValueError("the /verify/schema resource has no price or payment networks")
    version = doc.get("x402Version")
    return {
        "endpoint": resource["url"],
        "method": resource.get("method", "POST"),
        "price": resource["price"],
        "currency": resource.get("currency"),
        "networks": networks,
        "protocol": f"x402 v{version}" if version is not None else "x402",
    }


def parse_free_path(card: dict[str, Any]) -> dict[str, Any]:
    """The verify_schema MCP tool's free allowance out of the agent-card. Raises ValueError if absent."""
    extension = next(
        (e for e in card.get("capabilities", {}).get("extensions", []) if str(e.get("uri", "")).endswith("mcp:v1")),
        None,
    )
    if extension is None:
        raise ValueError("no MCP extension in the agent-card")
    params = extension.get("params", {})
    tool = next((t for t in params.get("tools", []) if t.get("toolName") == VERIFY_TOOL), None)
    access = (tool or {}).get("access") or params.get("access") or {}
    free = access.get("freeTrial")
    if not params.get("url") or not free:
        raise ValueError("the MCP extension has no url or free-trial allowance")
    return {
        "transport": params.get("transport", "streamable-http"),
        "url": params["url"],
        "tool": VERIFY_TOOL,
        "calls_per_client_per_day": free.get("callsPerClientPerDay"),
        "max_input_bytes": free.get("maxInputBytes"),
        "after_free_trial": access.get("afterFreeTrial"),
    }


# ---- state ----------------------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_from_db() -> None:
    """Last-known values from a previous process, marked not-live until this process
    confirms them. Best-effort: a missing/unreachable table just means nothing is known yet."""
    global _loaded_from_db
    _loaded_from_db = True
    try:
        stored = db.load_verifier_info()
    except Exception:  # noqa: BLE001
        logger.warning("could not load the last-known verifier info", exc_info=True)
        return
    with _lock:
        for name, section in (stored or {}).items():
            if name not in _sections and isinstance(section, dict) and "data" in section:
                _sections[name] = {"data": section["data"], "fetched_at": section.get("fetched_at"), "live": False}


def _save_to_db() -> None:
    with _lock:
        payload = {n: {"data": s["data"], "fetched_at": s["fetched_at"]} for n, s in _sections.items()}
    try:
        db.save_verifier_info(payload)
    except Exception:  # noqa: BLE001
        logger.warning("could not persist the verifier info", exc_info=True)


def refresh() -> bool:
    """Fetch both documents now (blocking). Each section updates independently; a failed one
    keeps its last-known value and is marked not-live. Returns True if both succeeded."""
    global _refreshing, _last_attempt, _last_attempt_ok
    if not _loaded_from_db:
        _load_from_db()
    ok = True
    for name, url, parse in (("payment", X402_URL, parse_payment), ("free_path", AGENT_CARD_URL, parse_free_path)):
        try:
            data = parse(_fetch_json(url))
        except Exception as exc:  # noqa: BLE001 - network, HTTP, JSON or shape: all the same to a caller
            ok = False
            logger.warning("verifier info: could not refresh %s from %s (%s); keeping the last-known value", name, url, exc)
            with _lock:
                if name in _sections:
                    _sections[name]["live"] = False
            continue
        with _lock:
            _sections[name] = {"data": data, "fetched_at": _now_iso(), "live": True}
    if ok:
        _save_to_db()
    elif any(s["live"] for s in _sections.values()):
        _save_to_db()
    with _lock:
        _last_attempt = time.monotonic()
        _last_attempt_ok = ok
        _refreshing = False
    return ok


def _refresh_in_background() -> None:
    global _refreshing
    try:
        refresh()
    except Exception:  # noqa: BLE001 - must never escape a daemon thread silently
        logger.exception("verifier info refresh crashed")
        with _lock:
            _refreshing = False


def _refresh_due() -> bool:
    if _last_attempt is None:
        return True
    wait = TTL_SECONDS if _last_attempt_ok else FAILURE_BACKOFF_SECONDS
    return time.monotonic() - _last_attempt >= wait


def start() -> None:
    """Called once at app startup: load the last-known values and refresh, off the boot path."""
    if not _refresh_enabled():
        return
    global _refreshing
    with _lock:
        if _refreshing:
            return
        _refreshing = True
    threading.Thread(target=_refresh_in_background, name="verifier-info-start", daemon=True).start()


def snapshot() -> dict[str, Any]:
    """What to put in a next_action right now. Never blocks on the network, never raises.

    {"payment": {...}|None, "free_path": {...}|None, "status": "live"|"last_known"|"partial"|
     "unavailable", "fetched_at": iso|None (the oldest of the sections present), "sources": [...]}
    """
    global _refreshing
    if _refresh_enabled():
        with _lock:
            start_one = not _refreshing and _refresh_due()
            if start_one:
                _refreshing = True
        if start_one:
            threading.Thread(target=_refresh_in_background, name="verifier-info-refresh", daemon=True).start()
    with _lock:
        payment, free = _sections.get("payment"), _sections.get("free_path")
        present = [s for s in (payment, free) if s is not None]
        if not present:
            status = "unavailable"
        elif len(present) < 2:
            status = "partial"
        elif all(s["live"] for s in present):
            status = "live"
        else:
            status = "last_known"
        fetched = sorted(s["fetched_at"] for s in present if s.get("fetched_at"))
        return {
            "payment": dict(payment["data"]) if payment else None,
            "free_path": dict(free["data"]) if free else None,
            "status": status,
            "fetched_at": fetched[0] if fetched else None,
            "sources": list(SOURCES),
        }


def reset_for_tests() -> None:
    global _loaded_from_db, _refreshing, _last_attempt, _last_attempt_ok
    with _lock:
        _sections.clear()
        _loaded_from_db = True  # tests never read the real table unless they ask
        _refreshing = False
        _last_attempt = None
        _last_attempt_ok = True
