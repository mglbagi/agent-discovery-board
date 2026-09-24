"""Safety rails shared by the demo/walkthrough scripts (worked_example.py, agent_view.py),
so a demo can never quietly leave junk in production data:

  * refuse any non-local target unless --allow-production is given;
  * every listing a demo creates is a temporary `test-` listing (hidden from browse and
    search, purged by the board after its TTL) with an endpoint that can never resolve
    (`https://test-<id>.example.invalid/...`; `.invalid` is a reserved TLD, RFC 2606);
  * DemoSession deactivates everything the demo created, in a `finally`, even when the
    demo fails halfway - the automatic purge is the backstop, not the plan.
"""

import base64
import json
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from eth_account.messages import encode_defunct  # noqa: E402

from app.core.demo_data import TEST_LISTING_TTL_HOURS, TEST_NAME_PREFIX  # noqa: E402
from app.core.wallet_auth import _build_message  # noqa: E402

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
REFUSED_EXIT_CODE = 2


def is_local_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in LOCAL_HOSTS


def require_safe_target(url: str, allow_production: bool) -> None:
    """Exit (code 2) before any request is made if `url` is not local and the caller did
    not explicitly pass --allow-production."""
    if is_local_url(url) or allow_production:
        return
    print(
        f"REFUSING to run against {url}: it is not a local address. Demos create (temporary, self-cleaning) "
        "test- listings; if you really do want them on this deployment, re-run with --allow-production.",
        file=sys.stderr,
    )
    raise SystemExit(REFUSED_EXIT_CODE)


def new_marker() -> str:
    return uuid.uuid4().hex[:8]


def demo_name(label: str, marker: str) -> str:
    return f"{TEST_NAME_PREFIX}{label}-{marker}"


def demo_endpoint(marker: str, path: str = "") -> str:
    return f"https://{TEST_NAME_PREFIX}{marker}.example.invalid{path}"


def make_client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=60)


def _sig_hex(signed) -> str:
    raw = signed.signature.hex()
    return raw if raw.startswith("0x") else "0x" + raw


def signed_header(account, *, action: str, listing_id: str, body: dict | None = None) -> dict[str, str]:
    """The X-Wallet-Auth header for one signed request, built with the board's own message
    builder (the published signing spec in the manifest describes the same thing)."""
    timestamp, nonce = int(time.time()), uuid.uuid4().hex
    message = _build_message(action=action, listing_id=listing_id, timestamp=timestamp, nonce=nonce, body=body)
    signed = account.sign_message(encode_defunct(text=message))
    payload = {"signature": _sig_hex(signed), "timestamp": timestamp, "nonce": nonce}
    return {"X-Wallet-Auth": base64.b64encode(json.dumps(payload).encode()).decode()}


class DemoSession:
    """Creates listings and guarantees they are cleaned up:

        with DemoSession(http) as demo:
            response = demo.create(payload, account)
            ...                      # whatever the demo does, even if it raises
        # <- every listing created above has been deactivated here
    """

    def __init__(self, http: httpx.Client, log=print) -> None:
        self.http = http
        self.log = log
        self.created: list[tuple[str, object]] = []
        self.cleanup_failures: list[str] = []

    def create(self, payload: dict, account) -> httpx.Response:
        if not payload["name"].startswith(TEST_NAME_PREFIX):
            raise ValueError(f"demo listings must be named {TEST_NAME_PREFIX}...: {payload['name']!r}")
        response = self.http.post("/listings", json=payload)
        if response.status_code == 201:
            self.created.append((response.json()["id"], account))
        return response

    def cleanup(self) -> None:
        for listing_id, account in reversed(self.created):
            try:
                response = self.http.delete(
                    f"/listings/{listing_id}", headers=signed_header(account, action="delete-listing", listing_id=listing_id)
                )
                ok = response.status_code == 200
                self.log(f"cleanup: deactivated {listing_id} -> {response.status_code}")
            except Exception as exc:  # noqa: BLE001 - cleanup must try every listing
                ok = False
                self.log(f"cleanup: FAILED for {listing_id}: {exc}")
            if not ok:
                self.cleanup_failures.append(listing_id)
        if self.cleanup_failures:
            self.log(
                f"cleanup: {len(self.cleanup_failures)} listing(s) could not be deactivated; they are hidden test "
                f"listings and the board purges them itself after ~{TEST_LISTING_TTL_HOURS:g}h: {self.cleanup_failures}"
            )
        self.created.clear()

    def __enter__(self) -> "DemoSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.cleanup()
        return False
