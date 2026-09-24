"""scripts/admin_update_listing.py: operator-only, dry run by default, guarded."""

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
from tests.helpers import db_row, listing_payload

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("admin_update_listing", ROOT / "scripts" / "admin_update_listing.py")
admin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admin)

client = TestClient(app)
OTHER_WALLET = "0x1111111111111111111111111111111111111111"


def _listing(**overrides):
    owner = Account.create()
    response = client.post("/listings", json=listing_payload("offering", owner.address, **overrides))
    assert response.status_code == 201, response.text
    return owner, response.json()


@pytest.fixture
def run(capsys, tmp_path):
    log_file = tmp_path / "admin.log"

    def _run(*argv: str, log: bool = True):
        args = list(argv) + (["--log-file", str(log_file)] if log else [])
        code = admin.main(args)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    _run.log_file = log_file
    return _run


def _log_lines(run) -> list[dict]:
    return [json.loads(line) for line in run.log_file.read_text(encoding="utf-8").splitlines()] if run.log_file.exists() else []


# ---- dry run is the default ----------------------------------------------------------------------


def test_default_is_a_dry_run_that_writes_nothing(run) -> None:
    owner, listing = _listing()
    before = db_row(listing["id"])
    code, out, err = run("--id", listing["id"], "--expect", f"name={listing['name']}", "--set", "name=Renamed")
    assert code == 0, err
    assert "DRY RUN" in out and "no changes written" in out
    assert "BEFORE" in out and "AFTER" in out and "Renamed" in out
    assert db_row(listing["id"]) == before
    assert _log_lines(run) == []


def test_apply_writes_only_the_named_field_bumps_updated_at_and_logs(run) -> None:
    owner, listing = _listing()
    before = db_row(listing["id"])
    code, out, err = run(
        "--id", listing["id"], "--expect", f"name={listing['name']}", "--set", "name=Renamed by operator", "--apply", "--yes"
    )
    assert code == 0, err
    assert "Applied" in out
    after = db_row(listing["id"])
    assert after["name"] == "Renamed by operator"
    assert after["updated_at"] > before["updated_at"]
    assert {k: v for k, v in after.items() if k not in ("name", "updated_at")} == {
        k: v for k, v in before.items() if k not in ("name", "updated_at")
    }

    (entry,) = _log_lines(run)
    assert entry["listing_id"] == listing["id"]
    assert entry["changes"] == {"name": {"before": listing["name"], "after": "Renamed by operator"}}
    assert entry["operator"] and entry["ts"] and entry["db"]


def test_apply_prints_the_row_after_and_appends_to_the_log_each_time(run) -> None:
    owner, listing = _listing()
    run("--id", listing["id"], "--expect", f"name={listing['name']}", "--set", "name=One", "--apply", "--yes")
    code, out, _ = run("--id", listing["id"], "--expect", "name=One", "--set", "name=Two", "--apply", "--yes")
    assert code == 0 and '"name": "Two"' in out
    assert [e["changes"]["name"]["after"] for e in _log_lines(run)] == ["One", "Two"]


# ---- confirmation ------------------------------------------------------------------------------------


def test_apply_asks_you_to_retype_the_id_and_a_wrong_answer_writes_nothing(run, monkeypatch) -> None:
    owner, listing = _listing()
    before = db_row(listing["id"])
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    code, _, err = run("--id", listing["id"], "--expect", f"name={listing['name']}", "--set", "name=Nope", "--apply")
    assert code == 2 and "nothing written" in err
    assert db_row(listing["id"]) == before and _log_lines(run) == []


def test_retyping_the_id_confirms(run, monkeypatch) -> None:
    owner, listing = _listing()
    monkeypatch.setattr("builtins.input", lambda prompt="": listing["id"])
    code, _, err = run("--id", listing["id"], "--expect", f"name={listing['name']}", "--set", "name=Confirmed", "--apply")
    assert code == 0, err
    assert db_row(listing["id"])["name"] == "Confirmed"


# ---- the guards ----------------------------------------------------------------------------------------


def test_a_wrong_expected_value_writes_nothing_even_with_apply(run) -> None:
    owner, listing = _listing()
    before = db_row(listing["id"])
    code, _, err = run("--id", listing["id"], "--expect", "name=Something else", "--set", "name=X", "--apply", "--yes")
    assert code == 2
    assert "expected 'Something else'" in err and listing["name"] in err
    assert db_row(listing["id"]) == before and _log_lines(run) == []


def test_at_least_one_expect_is_required(run) -> None:
    owner, listing = _listing()
    code, _, err = run("--id", listing["id"], "--set", "name=X", "--apply", "--yes")
    assert code == 1 and "--expect" in err
    assert db_row(listing["id"])["name"] == listing["name"]


def test_an_unknown_id_is_refused(run) -> None:
    code, _, err = run("--id", "00000000-0000-4000-8000-000000000000", "--expect", "name=x", "--set", "name=y", "--apply", "--yes")
    assert code == 2 and "exactly one listing" in err


def test_the_update_itself_is_guarded_against_a_change_made_after_the_read(run, monkeypatch) -> None:
    owner, listing = _listing()
    real_fetch = admin._fetch

    def racing_fetch(conn, listing_id):
        rows = real_fetch(conn, listing_id)
        with db._connection() as other:  # a concurrent writer sneaks in after our read
            other.execute("UPDATE listings SET status = 'inactive' WHERE id = %s", (listing_id,))
        return rows

    monkeypatch.setattr(admin, "_fetch", racing_fetch)
    code, _, err = run("--id", listing["id"], "--expect", "status=active", "--set", "name=Too late", "--apply", "--yes")
    assert code == 2 and "rolled back" in err
    row = db_row(listing["id"])
    assert row["name"] == listing["name"] and row["status"] == "inactive"
    assert _log_lines(run) == []


def test_the_resulting_listing_must_be_valid(run) -> None:
    owner, listing = _listing()
    code, _, err = run(
        "--id", listing["id"], "--expect", f"name={listing['name']}", "--set", "endpoint_url=http://insecure.example.com", "--apply", "--yes"
    )
    assert code == 1 and "invalid" in err
    assert db_row(listing["id"])["endpoint_url"] == listing["endpoint_url"]


def test_status_must_be_a_known_value(run) -> None:
    owner, listing = _listing()
    code, _, err = run("--id", listing["id"], "--expect", "status=active", "--set", "status=deleted", "--apply", "--yes")
    assert code == 1 and "status" in err
    assert db_row(listing["id"])["status"] == "active"


@pytest.mark.parametrize("field", ["id", "created_at", "updated_at", "last_seen_at", "endpoint_key", "nonsense"])
def test_only_editable_fields_can_be_set(run, field: str) -> None:
    owner, listing = _listing()
    code, _, err = run("--id", listing["id"], "--expect", "status=active", "--set", f"{field}=x", "--apply", "--yes")
    assert code == 1 and "field one of" in err


def test_a_duplicate_of_another_active_listing_is_refused(run) -> None:
    owner = Account.create()
    a = client.post("/listings", json=listing_payload("offering", owner.address)).json()
    b = client.post("/listings", json=listing_payload("offering", owner.address)).json()
    code, _, err = run(
        "--id", b["id"], "--expect", f"endpoint_url={b['endpoint_url']}", "--set", f"endpoint_url={a['endpoint_url']}/", "--apply", "--yes"
    )
    assert code == 2 and "another ACTIVE offering" in err
    assert db_row(b["id"])["endpoint_url"] == b["endpoint_url"]


# ---- what it can change -----------------------------------------------------------------------------------


def test_repointing_the_owner(run) -> None:
    owner, listing = _listing()
    code, _, err = run(
        "--id", listing["id"], "--expect", f"submitted_by={owner.address}", "--set", f"submitted_by={OTHER_WALLET}", "--apply", "--yes"
    )
    assert code == 0, err
    assert db_row(listing["id"])["submitted_by"] == OTHER_WALLET


def test_changing_the_endpoint_recomputes_the_duplicate_key(run) -> None:
    owner, listing = _listing()
    new_url = "https://Moved.Example.com/agents/new/"
    code, _, err = run("--id", listing["id"], "--expect", f"endpoint_url={listing['endpoint_url']}", "--set", f"endpoint_url={new_url}", "--apply", "--yes")
    assert code == 0, err
    with db._connection() as conn:
        key = conn.execute("SELECT endpoint_key FROM listings WHERE id = %s", (listing["id"],)).fetchone()["endpoint_key"]
    assert key == "https://moved.example.com/agents/new"
    assert db.find_active_duplicate("https://moved.example.com/agents/new", owner.address) == listing["id"]


def test_structured_fields_take_json(run) -> None:
    owner, listing = _listing()
    options = [{"network": "eip155:8453", "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "pay_to": owner.address}]
    code, _, err = run(
        "--id", listing["id"], "--expect", "status=active", "--set", f"payment_options={json.dumps(options)}",
        "--set", 'task_categories=["translation", "summarization"]', "--apply", "--yes",
    )
    assert code == 0, err
    row = db_row(listing["id"])
    # stored in the same normalized form the API stores (explicit nulls for omitted amount/unit)
    assert row["payment_options"] == [{**options[0], "amount": None, "unit": None}]
    assert row["task_categories"] == ["translation", "summarization"]


def test_bad_json_is_refused(run) -> None:
    owner, listing = _listing()
    code, _, err = run("--id", listing["id"], "--expect", "status=active", "--set", "payment_options=[oops", "--apply", "--yes")
    assert code == 1 and "JSON" in err


def test_nullable_fields_can_be_unset_together_but_not_alone_when_they_depend_on_each_other(run) -> None:
    owner, listing = _listing()
    code, _, err = run("--id", listing["id"], "--expect", "status=active", "--unset", "pricing_amount", "--apply", "--yes")
    assert code == 1 and "invalid" in err  # pricing_model still needs an amount

    code, _, err = run(
        "--id", listing["id"], "--expect", "status=active", "--unset", "pricing_amount", "--unset", "pricing_model", "--apply", "--yes"
    )
    assert code == 0, err
    row = db_row(listing["id"])
    assert row["pricing_amount"] is None and row["pricing_model"] is None


def test_only_nullable_fields_can_be_unset_and_set_plus_unset_conflicts(run) -> None:
    owner, listing = _listing()
    code, _, err = run("--id", listing["id"], "--expect", "status=active", "--unset", "name")
    assert code == 1 and "nullable" in err
    code, _, err = run("--id", listing["id"], "--expect", "status=active", "--set", "pricing_amount=$1", "--unset", "pricing_amount")
    assert code == 1 and "both" in err


def test_nothing_to_do_and_no_op_changes_are_reported(run) -> None:
    owner, listing = _listing()
    code, _, err = run("--id", listing["id"], "--expect", "status=active")
    assert code == 1 and "nothing to do" in err

    code, out, _ = run("--id", listing["id"], "--expect", "status=active", "--set", f"name={listing['name']}", "--apply", "--yes")
    assert code == 0 and "nothing to change" in out and _log_lines(run) == []


# ---- hygiene ------------------------------------------------------------------------------------------------


def test_the_target_is_shown_but_never_the_credentials(run) -> None:
    owner, listing = _listing()
    url = urlparse(os.environ["DATABASE_URL"])
    code, out, err = run("--id", listing["id"], "--expect", "status=active", "--set", "name=Hygiene", "--apply", "--yes")
    assert code == 0
    assert url.hostname in out
    assert url.password and url.password not in out + err
    assert run.log_file.read_text(encoding="utf-8").count(url.password) == 0


def test_a_missing_database_url_is_a_loud_failure(run, monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL")
    code, _, err = run("--id", "x", "--expect", "name=x", "--set", "name=y")
    assert code == 1 and "DATABASE_URL" in err
