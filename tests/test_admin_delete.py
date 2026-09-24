"""scripts/admin_update_listing.py --delete: guarded HARD delete of one inactive listing."""

import importlib.util
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app.core import db
from app.main import app
from tests.helpers import db_row, listing_payload, wallet_auth_header

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("admin_update_listing", ROOT / "scripts" / "admin_update_listing.py")
admin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admin)

client = TestClient(app)


def _inactive_listing(**overrides):
    owner = Account.create()
    listing = client.post("/listings", json=listing_payload("offering", owner.address, **overrides)).json()
    header = wallet_auth_header(owner, action="delete-listing", listing_id=listing["id"])
    assert client.delete(f"/listings/{listing['id']}", headers={"X-Wallet-Auth": header}).status_code == 200
    return owner, listing


@pytest.fixture
def run(capsys, tmp_path):
    log_file = tmp_path / "admin.log"

    def _run(*argv: str):
        code = admin.main(list(argv) + ["--log-file", str(log_file)])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    _run.log_file = log_file
    return _run


def _exists(listing_id: str) -> bool:
    return db_row(listing_id) is not None


def _log(run) -> list[dict]:
    return [json.loads(l) for l in run.log_file.read_text(encoding="utf-8").splitlines()] if run.log_file.exists() else []


def test_dry_run_is_the_default_and_deletes_nothing(run) -> None:
    owner, listing = _inactive_listing()
    code, out, err = run("--id", listing["id"], "--expect", "status=inactive", "--expect", f"name={listing['name']}", "--delete")
    assert code == 0, err
    assert "HARD DELETE" in out and "DRY RUN" in out and "nothing deleted" in out and listing["id"] in out
    assert _exists(listing["id"]) and _log(run) == []


def test_apply_deletes_exactly_that_row_and_logs_the_whole_row(run) -> None:
    owner, listing = _inactive_listing()
    bystander_owner, bystander = _inactive_listing()  # another inactive listing must survive
    before = db_row(listing["id"])

    code, out, err = run(
        "--id", listing["id"], "--expect", "status=inactive", "--expect", f"name={listing['name']}", "--delete", "--apply", "--yes"
    )
    assert code == 0, err
    assert "Deleted listing" in out
    assert not _exists(listing["id"]) and _exists(bystander["id"])
    assert client.get(f"/listings/{listing['id']}").status_code == 404

    (entry,) = _log(run)
    assert entry["action"] == "hard-delete" and entry["listing_id"] == listing["id"] and entry["operator"]
    assert entry["deleted_row"]["id"] == listing["id"]
    assert entry["deleted_row"]["endpoint_url"] == before["endpoint_url"]  # enough to reconstruct the row


def test_an_active_listing_is_refused_even_with_apply(run) -> None:
    owner = Account.create()
    listing = client.post("/listings", json=listing_payload("offering", owner.address)).json()
    code, _, err = run("--id", listing["id"], "--expect", "status=active", "--delete", "--apply", "--yes")
    assert code == 2 and "not inactive" in err
    assert _exists(listing["id"]) and _log(run) == []


def test_a_wrong_expected_value_deletes_nothing(run) -> None:
    owner, listing = _inactive_listing()
    code, _, err = run("--id", listing["id"], "--expect", "name=Some other listing", "--delete", "--apply", "--yes")
    assert code == 2 and "expected 'Some other listing'" in err
    assert _exists(listing["id"]) and _log(run) == []


def test_at_least_one_expect_is_required(run) -> None:
    owner, listing = _inactive_listing()
    code, _, err = run("--id", listing["id"], "--delete", "--apply", "--yes")
    assert code == 1 and "--expect" in err and _exists(listing["id"])


def test_an_unknown_id_is_refused(run) -> None:
    code, _, err = run("--id", "00000000-0000-4000-8000-000000000000", "--expect", "status=inactive", "--delete", "--apply", "--yes")
    assert code == 2 and "exactly one listing" in err


def test_delete_cannot_be_combined_with_set_or_unset(run) -> None:
    owner, listing = _inactive_listing()
    code, _, err = run("--id", listing["id"], "--expect", "status=inactive", "--set", "name=x", "--delete")
    assert code == 1 and "cannot be combined" in err
    code, _, err = run("--id", listing["id"], "--expect", "status=inactive", "--unset", "pricing_amount", "--delete")
    assert code == 1 and "cannot be combined" in err
    assert _exists(listing["id"])


def test_apply_asks_for_the_id_and_a_wrong_answer_deletes_nothing(run, monkeypatch) -> None:
    owner, listing = _inactive_listing()
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    code, _, err = run("--id", listing["id"], "--expect", "status=inactive", "--delete", "--apply")
    assert code == 2 and "nothing written" in err and _exists(listing["id"])

    monkeypatch.setattr("builtins.input", lambda prompt="": listing["id"])
    code, _, err = run("--id", listing["id"], "--expect", "status=inactive", "--delete", "--apply")
    assert code == 0, err
    assert not _exists(listing["id"])


def test_the_delete_itself_is_guarded_against_a_change_after_the_read(run, monkeypatch) -> None:
    owner, listing = _inactive_listing()
    real_fetch = admin._fetch
    calls = {"n": 0}

    def racing_fetch(conn, listing_id):
        rows = real_fetch(conn, listing_id)
        calls["n"] += 1
        if calls["n"] == 1:  # after our read, someone reactivates the listing
            with db._connection() as other:
                other.execute("UPDATE listings SET status = 'active' WHERE id = %s", (listing_id,))
        return rows

    monkeypatch.setattr(admin, "_fetch", racing_fetch)
    code, _, err = run("--id", listing["id"], "--expect", "status=inactive", "--delete", "--apply", "--yes")
    assert code == 2 and "rolled back" in err
    assert db_row(listing["id"])["status"] == "active"  # untouched by us, and not deleted
    assert _log(run) == []


def test_credentials_are_never_printed(run) -> None:
    owner, listing = _inactive_listing()
    url = urlparse(os.environ["DATABASE_URL"])
    code, out, err = run("--id", listing["id"], "--expect", "status=inactive", "--delete", "--apply", "--yes")
    assert code == 0 and url.hostname in out
    assert url.password not in out + err + run.log_file.read_text(encoding="utf-8")
