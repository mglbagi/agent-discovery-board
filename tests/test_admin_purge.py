"""scripts/admin_update_listing.py --purge-test (the fallback for the automatic purge) and
the name-prefix rule for direct edits."""

import importlib.util
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app.core import db, maintenance
from app.main import app
from tests.helpers import db_row, listing_payload, set_listing_times

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("admin_update_listing", ROOT / "scripts" / "admin_update_listing.py")
admin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admin)

client = TestClient(app)


def _wipe_test_rows() -> None:
    db.purge_expired_test_listings(datetime.now(timezone.utc) + timedelta(days=3650), 100_000)


@pytest.fixture(autouse=True)
def _clean():
    _wipe_test_rows()
    yield
    _wipe_test_rows()


@pytest.fixture
def run(capsys, tmp_path):
    log_file = tmp_path / "admin.log"

    def _run(*argv: str):
        code = admin.main(list(argv) + ["--log-file", str(log_file)])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    _run.log_file = log_file
    return _run


def _log(run) -> list[dict]:
    return [json.loads(l) for l in run.log_file.read_text(encoding="utf-8").splitlines()] if run.log_file.exists() else []


def _make(name: str, **overrides) -> dict:
    owner = Account.create()
    response = client.post("/listings", json=listing_payload("offering", owner.address, name=name, **overrides))
    assert response.status_code == 201, response.text
    return response.json()


def _tname() -> str:
    return f"test-{uuid.uuid4().hex[:10]}"


def _setup():
    expired = _make(_tname())
    fresh = _make(_tname())
    real_old = _make("real old " + uuid.uuid4().hex[:6])
    set_listing_times(expired["id"], created_days_ago=3)
    set_listing_times(real_old["id"], created_days_ago=4000)
    return expired, fresh, real_old


def test_dry_run_lists_expired_test_listings_and_deletes_nothing(run) -> None:
    expired, fresh, real_old = _setup()
    code, out, err = run("--purge-test")
    assert code == 0, err
    assert "PURGE TEST LISTINGS" in out and "DRY RUN" in out and "nothing deleted" in out
    assert expired["id"] in out and fresh["id"] not in out and real_old["id"] not in out
    assert all(db_row(x["id"]) for x in (expired, fresh, real_old)) and _log(run) == []


def test_apply_deletes_only_expired_test_listings_and_logs_them(run) -> None:
    expired, fresh, real_old = _setup()
    code, out, err = run("--purge-test", "--apply", "--yes")
    assert code == 0, err
    assert db_row(expired["id"]) is None
    assert db_row(fresh["id"]) is not None and db_row(real_old["id"]) is not None

    (entry,) = _log(run)
    assert entry["action"] == "purge-test" and entry["older_than_hours"] > 0
    assert [r["id"] for r in entry["deleted_rows"]] == [expired["id"]]
    assert entry["deleted_rows"][0]["is_test"] is True


def test_older_than_zero_takes_every_test_listing_but_never_a_real_one(run) -> None:
    expired, fresh, real_old = _setup()
    code, out, err = run("--purge-test", "--older-than-hours", "0", "--apply", "--yes")
    assert code == 0, err
    assert db_row(expired["id"]) is None and db_row(fresh["id"]) is None
    assert db_row(real_old["id"]) is not None


def test_an_explicit_age_threshold_is_respected(run) -> None:
    expired, fresh, _ = _setup()  # expired is 3 days old, fresh is new
    code, _, err = run("--purge-test", "--older-than-hours", "100", "--apply", "--yes")  # 3 days = 72h is NOT older than 100h
    assert code == 0, err
    assert db_row(expired["id"]) is not None and db_row(fresh["id"]) is not None
    code, _, err = run("--purge-test", "--older-than-hours", "48", "--apply", "--yes")  # 72h is older than 48h
    assert code == 0, err
    assert db_row(expired["id"]) is None and db_row(fresh["id"]) is not None
    code, _, err = run("--purge-test", "--older-than-hours", "-1")
    assert code == 1 and ">= 0" in err


def test_limit_bounds_a_run(run) -> None:
    items = [_make(_tname()) for _ in range(4)]
    for item in items:
        set_listing_times(item["id"], created_days_ago=3)
    code, out, err = run("--purge-test", "--limit", "3", "--apply", "--yes")
    assert code == 0 and "Deleted 3 test listing(s)" in out
    assert sum(db_row(i["id"]) is not None for i in items) == 1
    code, _, err = run("--purge-test", "--limit", "0")
    assert code == 1 and "--limit" in err


def test_apply_asks_for_the_word_purge(run, monkeypatch) -> None:
    expired, _, _ = _setup()
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    code, _, err = run("--purge-test", "--apply")
    assert code == 2 and "nothing written" in err and db_row(expired["id"]) is not None

    monkeypatch.setattr("builtins.input", lambda prompt="": "PURGE")
    code, _, err = run("--purge-test", "--apply")
    assert code == 0, err and db_row(expired["id"]) is None


def test_nothing_to_purge_is_reported_and_logs_nothing(run) -> None:
    _make(_tname())  # fresh
    code, out, _ = run("--purge-test", "--apply", "--yes")
    assert code == 0 and "Nothing to purge" in out and _log(run) == []


@pytest.mark.parametrize(
    "extra",
    [["--id", "x"], ["--expect", "name=x"], ["--set", "name=x"], ["--unset", "pricing_amount"], ["--delete"]],
)
def test_purge_stands_alone(run, extra) -> None:
    expired, _, _ = _setup()
    code, _, err = run("--purge-test", "--apply", "--yes", *extra)
    assert code == 1 and "stands alone" in err and db_row(expired["id"]) is not None


def test_an_id_is_still_required_without_purge(run) -> None:
    with pytest.raises(SystemExit) as exc:
        admin.main(["--expect", "name=x", "--set", "name=y"])
    assert exc.value.code == 2


def test_credentials_are_never_printed(run) -> None:
    _setup()
    url = urlparse(os.environ["DATABASE_URL"])
    code, out, err = run("--purge-test", "--apply", "--yes")
    assert code == 0 and url.hostname in out
    assert url.password not in out + err + run.log_file.read_text(encoding="utf-8")


# ---- direct edits obey the same name-prefix rule ------------------------------------------------------------


def test_the_operator_script_cannot_turn_a_real_listing_into_a_test_listing(run) -> None:
    real = _make("real-" + uuid.uuid4().hex[:8])
    code, _, err = run("--id", real["id"], "--expect", "status=active", "--set", f"name={_tname()}", "--apply", "--yes")
    assert code == 1 and "invalid_test_name" in err
    assert db_row(real["id"])["name"] == real["name"] and db_row(real["id"])["is_test"] is False


def test_nor_strip_the_prefix_from_a_test_listing(run) -> None:
    listing = _make(_tname())
    code, _, err = run("--id", listing["id"], "--expect", "status=active", "--set", "name=an ordinary name", "--apply", "--yes")
    assert code == 1 and "invalid_test_name" in err
    assert db_row(listing["id"])["is_test"] is True


def test_other_edits_to_a_test_listing_are_fine(run) -> None:
    listing = _make(_tname())
    code, _, err = run("--id", listing["id"], "--expect", "status=active", "--set", "description=edited", "--apply", "--yes")
    assert code == 0, err and db_row(listing["id"])["description"] == "edited"
