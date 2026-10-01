"""scripts/bulk_import_listings.py: operator-only, dry run by default, upserts by
(source, endpoint), honors the do-not-import list, never overwrites a claimed listing,
and marks missing listings stale."""

import importlib.util
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app.core import db
from app.core.activity import is_stale
from app.main import app
from tests.helpers import wallet_auth_header

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("bulk_import_listings", ROOT / "scripts" / "bulk_import_listings.py")
bulk_import = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bulk_import)

client = TestClient(app)


def _record(name: str | None = None, **overrides) -> dict:
    owner = Account.create()
    base = {
        "name": f"Imported Agent {uuid.uuid4().hex[:8]}" if name is None else name,
        "description": "An agent imported for a test.",
        "task_categories": ["other"],
        "endpoint_url": f"https://example.com/bulk/{uuid.uuid4().hex}",
        "payment_wallet": owner.address,
        "pricing_model": "per_call",
        "pricing_amount": "$0.01",
    }
    base.update(overrides)
    return base


def _source() -> str:
    return "bulk-" + uuid.uuid4().hex[:8]


@pytest.fixture
def run(capsys, tmp_path):
    log_file = tmp_path / "bulk.log"

    def _run(records: list[dict], *argv: str, log: bool = True):
        records_file = tmp_path / f"records-{uuid.uuid4().hex[:6]}.json"
        records_file.write_text(json.dumps(records), encoding="utf-8")
        args = ["--file", str(records_file)] + list(argv) + (["--log-file", str(log_file)] if log else [])
        code = bulk_import.main(args)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    _run.log_file = log_file
    return _run


def _log_lines(run) -> list[dict]:
    return [json.loads(line) for line in run.log_file.read_text(encoding="utf-8").splitlines()] if run.log_file.exists() else []


# ---- dry run is the default ------------------------------------------------------------------


def test_default_is_a_dry_run_that_writes_nothing(run) -> None:
    source = _source()
    record = _record()
    code, out, err = run([record], "--source", source)
    assert code == 0, err
    assert "DRY RUN" in out and "1 record(s) would be inserted or updated" in out
    assert _log_lines(run) == []
    page = client.get("/listings", params={"q": record["name"]})
    assert page.json()["total"] == 0


def test_apply_inserts_new_unclaimed_listings_and_logs(run) -> None:
    source = _source()
    record = _record()
    code, out, err = run([record], "--source", source, "--apply", "--yes")
    assert code == 0, err
    assert "Inserted 1, updated 0" in out

    rows = [db.get_listing(entry["id"]) for entry in _log_lines(run)[0]["inserted"]]
    assert len(rows) == 1
    assert rows[0]["name"] == record["name"]
    assert rows[0]["claimed"] is False
    assert rows[0]["source"] == source
    assert rows[0]["listing_type"] == "verification_profile"


# ---- validation: one bad record aborts the whole batch --------------------------------------


def test_an_invalid_record_aborts_the_whole_batch(run) -> None:
    good = _record()
    bad = _record(name="")  # empty name fails ListingCreate validation
    code, out, err = run([good, bad], "--source", _source(), "--apply", "--yes")
    assert code != 0
    assert "invalid" in err.lower()
    # Not "q=good['name'], total==0": the trigram fallback can fuzzy-match an unrelated
    # listing sharing the "Imported Agent" prefix from another test in this shared
    # database. Checking by the (unique, random) endpoint_url is precise either way.
    page = client.get("/listings", params={"listing_type": "verification_profile", "limit": 100}).json()
    assert not any(item["endpoint_url"] == good["endpoint_url"] for item in page["listings"])


def test_a_non_object_record_aborts_cleanly(run) -> None:
    code, out, err = run(["not an object"], "--source", _source())
    assert code != 0
    assert "expected an object" in err


# ---- re-sync: update in place, never duplicate -----------------------------------------------


def test_resyncing_the_same_endpoint_updates_rather_than_duplicates(run) -> None:
    source = _source()
    record = _record(name="Original")
    run([record], "--source", source, "--apply", "--yes")

    updated_record = {**record, "name": "Refreshed", "description": "refreshed text"}
    code, out, err = run([updated_record], "--source", source, "--apply", "--yes")
    assert code == 0, err
    assert "Inserted 0, updated 1" in out

    page = client.get("/listings", params={"listing_type": "verification_profile", "q": "Refreshed"}).json()
    assert any(item["endpoint_url"] == record["endpoint_url"] for item in page["listings"])
    all_matching = client.get(
        "/listings", params={"listing_type": "verification_profile", "limit": 100}
    ).json()["listings"]
    same_endpoint = [i for i in all_matching if i["endpoint_url"] == record["endpoint_url"]]
    assert len(same_endpoint) == 1  # not duplicated
    assert same_endpoint[0]["name"] == "Refreshed"


def test_resync_never_overwrites_a_claimed_listings_content(run) -> None:
    source = _source()
    owner = Account.create()
    record2 = _record(name="Claimable", payment_wallet=owner.address)
    run([record2], "--source", source, "--apply", "--yes")
    claimable = client.get(
        "/listings", params={"listing_type": "verification_profile", "q": "Claimable"}
    ).json()["listings"][0]

    claim_header = wallet_auth_header(owner, action="claim-listing", listing_id=claimable["id"])
    claim_response = client.post(f"/listings/{claimable['id']}/claim", headers={"X-Wallet-Auth": claim_header})
    assert claim_response.status_code == 200

    patch_header = wallet_auth_header(owner, action="update-listing", listing_id=claimable["id"], body={"name": "Owner Edited"})
    client.patch(f"/listings/{claimable['id']}", json={"name": "Owner Edited"}, headers={"X-Wallet-Auth": patch_header})

    stale_resync = {**record2, "name": "Claimable", "description": "the source still has old content"}
    run([stale_resync], "--source", source, "--apply", "--yes")

    after = client.get(f"/listings/{claimable['id']}").json()
    assert after["name"] == "Owner Edited"
    assert after["claimed"] is True


# ---- do-not-import list -----------------------------------------------------------------------


def test_removed_listing_is_skipped_on_the_next_sync(run) -> None:
    source = _source()
    record = _record(name="To Be Removed")
    run([record], "--source", source, "--apply", "--yes")
    listing = client.get(
        "/listings", params={"listing_type": "verification_profile", "q": "To Be Removed"}
    ).json()["listings"][0]

    # Removed directly at the db layer here (same effect as the signed endpoint - see
    # tests/test_imports.py for that path) since this test doesn't hold the matching
    # private key for the random payment_wallet _record() generated.
    removed = db.remove_imported_listing(listing["id"], "test removal", datetime.now(timezone.utc))
    assert removed is not None

    code, out, err = run([record], "--source", source, "--apply", "--yes")
    assert code == 0, err
    assert "1 record(s) skipped (on the do-not-import list)" in out
    assert "Inserted 0, updated 0" in out
    page = client.get("/listings", params={"listing_type": "verification_profile", "q": "To Be Removed"}).json()
    assert page["total"] == 0


# ---- marking missing from source ---------------------------------------------------------------


def test_a_listing_absent_from_the_next_sync_is_marked_missing(run) -> None:
    source = _source()
    record = _record(name="Here Today")
    run([record], "--source", source, "--apply", "--yes")
    listing = client.get(
        "/listings", params={"listing_type": "verification_profile", "q": "Here Today"}
    ).json()["listings"][0]
    assert listing["stale"] is False

    other_record = _record(name="Unrelated Other")
    code, out, err = run([other_record], "--source", source, "--apply", "--yes")
    assert code == 0, err
    assert "1 previously-imported listing(s)" in out and "newly marked missing 1" in out

    refreshed = client.get(f"/listings/{listing['id']}").json()
    assert refreshed["stale"] is True


def test_dry_run_previews_missing_without_writing(run) -> None:
    source = _source()
    record = _record(name="Present Now")
    run([record], "--source", source, "--apply", "--yes")
    listing = client.get(
        "/listings", params={"listing_type": "verification_profile", "q": "Present Now"}
    ).json()["listings"][0]

    code, out, err = run([], "--source", source)  # dry run, empty sync
    assert code == 0, err
    assert "1 previously-imported listing(s)" in out
    assert client.get(f"/listings/{listing['id']}").json()["stale"] is False  # untouched


# ---- confirmation -------------------------------------------------------------------------------


def test_apply_without_yes_requires_retyping_the_source(run, monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": "wrong")
    source = _source()
    record = _record(name="Should Not Be Written")
    code, out, err = run([record], "--source", source, "--apply")
    assert code != 0
    assert "confirmation did not match" in err.lower()
    page = client.get("/listings", params={"listing_type": "verification_profile", "q": "Should Not Be Written"}).json()
    assert page["total"] == 0


def test_retyping_the_source_confirms(run, monkeypatch) -> None:
    source = _source()
    monkeypatch.setattr("builtins.input", lambda prompt="": source)
    code, out, err = run([_record()], "--source", source, "--apply")
    assert code == 0, err
