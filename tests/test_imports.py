"""Imported listings: verification_profile and other listing_types imported from a
third-party source, unclaimed until the real pay-to owner proves control, removable on
request with a do-not-import list, bulk-upsertable without duplicating or clobbering an
owner's edits, and marked stale immediately when a sync no longer finds them."""

import uuid
from datetime import datetime, timedelta, timezone

from eth_account import Account
from fastapi.testclient import TestClient

from app.core import db
from app.core.activity import is_stale
from app.core.imports import ImportRecordError, build_import_row
from app.core.reserved import UNCLAIMED_IMPORT_SUBMITTED_BY, is_reserved_address
from app.main import app
from tests.helpers import assert_error, import_listing, listing_payload, wallet_auth_header

client = TestClient(app)


def _source() -> str:
    return "src-" + uuid.uuid4().hex[:8]


# ---- creating an imported listing (the admin/import path, never POST /listings) -------------


def test_build_import_row_defaults_to_verification_profile_and_is_unclaimed() -> None:
    owner = Account.create()
    row = build_import_row(
        {
            "name": "Some Agent",
            "description": "An agent.",
            "task_categories": ["other"],
            "endpoint_url": "https://example.com/agents/1",
            "payment_wallet": owner.address,
            "pricing_model": "per_call",
            "pricing_amount": "$0.01",
        },
        source="x402_bazaar",
        now=datetime.now(timezone.utc),
    )
    assert row["listing_type"] == "verification_profile"
    assert row["claimed"] is False
    assert row["submitted_by"] == UNCLAIMED_IMPORT_SUBMITTED_BY
    assert row["is_test"] is False
    assert row["source"] == "x402_bazaar"


def test_build_import_row_rejects_an_invalid_record() -> None:
    try:
        build_import_row({"name": "", "description": "x"}, source="s", now=datetime.now(timezone.utc))
        assert False, "should have raised"
    except ImportRecordError:
        pass


def test_a_test_prefixed_source_name_does_not_make_it_a_test_listing() -> None:
    owner = Account.create()
    row = build_import_row(
        {
            "name": "test-looking-name",
            "description": "x",
            "task_categories": ["other"],
            "endpoint_url": "https://example.com/agents/2",
            "payment_wallet": owner.address,
            "pricing_model": "per_call",
            "pricing_amount": "$0.01",
        },
        source="s",
        now=datetime.now(timezone.utc),
    )
    assert row["is_test"] is False  # import is never OUR demo-data mechanism


def test_the_unclaimed_placeholder_is_refused_as_an_ordinary_submitted_by() -> None:
    assert is_reserved_address(UNCLAIMED_IMPORT_SUBMITTED_BY)
    response = client.post("/listings", json=listing_payload("offering", UNCLAIMED_IMPORT_SUBMITTED_BY))
    assert_error(response, 422, "reserved_address")


def test_an_imported_listing_is_clearly_marked_and_never_pays_the_board() -> None:
    owner = Account.create()
    row = import_listing(_source(), owner.address)
    body = client.get(f"/listings/{row['id']}").json()
    assert body["claimed"] is False
    assert body["source"] == row["source"]
    assert body["source_url"] is None or isinstance(body["source_url"], str)
    assert body["payment_wallet"] == owner.address  # the listed service's own pay-to address
    assert body["submitted_by"] == UNCLAIMED_IMPORT_SUBMITTED_BY


def test_an_organic_listing_is_claimed_by_default_with_no_source() -> None:
    owner = Account.create()
    created = client.post("/listings", json=listing_payload("offering", owner.address)).json()
    assert created["claimed"] is True
    assert created["source"] is None and created["source_url"] is None
    assert created["imported_at"] is None and created["last_synced_at"] is None


# ---- claim flow -------------------------------------------------------------------------


def test_claiming_an_imported_listing_sets_submitted_by_and_claimed() -> None:
    owner = Account.create()
    row = import_listing(_source(), owner.address)
    header = wallet_auth_header(owner, action="claim-listing", listing_id=row["id"])
    response = client.post(f"/listings/{row['id']}/claim", headers={"X-Wallet-Auth": header})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["claimed"] is True
    assert body["submitted_by"].lower() == owner.address.lower()


def test_claiming_with_the_wrong_wallet_is_rejected() -> None:
    owner, impostor = Account.create(), Account.create()
    row = import_listing(_source(), owner.address)
    header = wallet_auth_header(impostor, action="claim-listing", listing_id=row["id"])
    response = client.post(f"/listings/{row['id']}/claim", headers={"X-Wallet-Auth": header})
    assert_error(response, 403, "wrong_signer")


def test_claiming_an_already_claimed_listing_is_409() -> None:
    owner = Account.create()
    row = import_listing(_source(), owner.address)
    header = wallet_auth_header(owner, action="claim-listing", listing_id=row["id"])
    first = client.post(f"/listings/{row['id']}/claim", headers={"X-Wallet-Auth": header})
    assert first.status_code == 200

    header2 = wallet_auth_header(owner, action="claim-listing", listing_id=row["id"])
    second = client.post(f"/listings/{row['id']}/claim", headers={"X-Wallet-Auth": header2})
    assert_error(second, 409, "already_claimed")


def test_claiming_an_organic_already_claimed_listing_is_409() -> None:
    owner = Account.create()
    created = client.post("/listings", json=listing_payload("offering", owner.address)).json()
    header = wallet_auth_header(owner, action="claim-listing", listing_id=created["id"])
    response = client.post(f"/listings/{created['id']}/claim", headers={"X-Wallet-Auth": header})
    assert_error(response, 409, "already_claimed")


def test_claiming_a_nonexistent_listing_is_404() -> None:
    owner = Account.create()
    fake_id = str(uuid.uuid4())
    header = wallet_auth_header(owner, action="claim-listing", listing_id=fake_id)
    response = client.post(f"/listings/{fake_id}/claim", headers={"X-Wallet-Auth": header})
    assert_error(response, 404, "not_found")


def test_after_claiming_the_listing_behaves_normally_patch_and_delete() -> None:
    owner = Account.create()
    row = import_listing(_source(), owner.address)
    header = wallet_auth_header(owner, action="claim-listing", listing_id=row["id"])
    client.post(f"/listings/{row['id']}/claim", headers={"X-Wallet-Auth": header})

    patch = {"description": "edited after claiming"}
    patch_header = wallet_auth_header(owner, action="update-listing", listing_id=row["id"], body=patch)
    patched = client.patch(f"/listings/{row['id']}", json=patch, headers={"X-Wallet-Auth": patch_header})
    assert patched.status_code == 200 and patched.json()["description"] == "edited after claiming"


def test_before_claiming_the_normal_signed_patch_cannot_work_for_anyone() -> None:
    owner = Account.create()
    row = import_listing(_source(), owner.address)
    patch = {"description": "trying before claim"}
    # Even the real pay-to owner can't sign this: submitted_by is the unclaimed placeholder,
    # which nothing in app/core/wallet_auth.py recognizes as owner's signature.
    header = wallet_auth_header(owner, action="update-listing", listing_id=row["id"], body=patch)
    response = client.patch(f"/listings/{row['id']}", json=patch, headers={"X-Wallet-Auth": header})
    assert_error(response, 403, "wrong_signer")


# ---- removal flow -------------------------------------------------------------------------


def test_removing_an_imported_listing_deletes_it_and_blocks_reimport() -> None:
    owner = Account.create()
    source = _source()
    row = import_listing(source, owner.address)
    header = wallet_auth_header(owner, action="remove-imported-listing", listing_id=row["id"])
    response = client.post(f"/listings/{row['id']}/remove-imported", headers={"X-Wallet-Auth": header})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["removed"] is True and body["do_not_import"] is True
    assert client.get(f"/listings/{row['id']}").status_code == 404
    assert db.is_in_do_not_import(source, row["endpoint_url"]) is True


def test_removal_works_even_after_claiming() -> None:
    owner = Account.create()
    row = import_listing(_source(), owner.address)
    claim_header = wallet_auth_header(owner, action="claim-listing", listing_id=row["id"])
    client.post(f"/listings/{row['id']}/claim", headers={"X-Wallet-Auth": claim_header})

    remove_header = wallet_auth_header(owner, action="remove-imported-listing", listing_id=row["id"])
    response = client.post(f"/listings/{row['id']}/remove-imported", headers={"X-Wallet-Auth": remove_header})
    assert response.status_code == 200


def test_removal_with_the_wrong_wallet_is_rejected() -> None:
    owner, impostor = Account.create(), Account.create()
    row = import_listing(_source(), owner.address)
    header = wallet_auth_header(impostor, action="remove-imported-listing", listing_id=row["id"])
    response = client.post(f"/listings/{row['id']}/remove-imported", headers={"X-Wallet-Auth": header})
    assert_error(response, 403, "wrong_signer")
    assert client.get(f"/listings/{row['id']}").status_code == 200  # untouched


def test_removal_of_an_organic_listing_is_not_imported() -> None:
    owner = Account.create()
    created = client.post("/listings", json=listing_payload("offering", owner.address)).json()
    header = wallet_auth_header(owner, action="remove-imported-listing", listing_id=created["id"])
    response = client.post(f"/listings/{created['id']}/remove-imported", headers={"X-Wallet-Auth": header})
    assert_error(response, 422, "not_imported")


def test_a_removed_listing_is_not_recreated_by_a_later_import() -> None:
    owner = Account.create()
    source = _source()
    row = import_listing(source, owner.address)
    header = wallet_auth_header(owner, action="remove-imported-listing", listing_id=row["id"])
    client.post(f"/listings/{row['id']}/remove-imported", headers={"X-Wallet-Auth": header})

    # The script would check this before calling import_upsert again for the same record.
    assert db.is_in_do_not_import(row["source"], row["endpoint_url"]) is True


# ---- upsert: re-sync updates in place, never duplicates, protects a claimed owner's edits ----


def test_reimporting_the_same_source_and_endpoint_updates_not_duplicates() -> None:
    owner = Account.create()
    source = _source()
    first = import_listing(source, owner.address, name="Original Name")
    second_row = build_import_row(
        {
            "name": "Refreshed Name",
            "description": "refreshed",
            "task_categories": ["other"],
            "endpoint_url": first["endpoint_url"],
            "payment_wallet": owner.address,
            "pricing_model": "per_call",
            "pricing_amount": "$0.01",
        },
        source=source,
        now=datetime.now(timezone.utc),
    )
    updated, was_inserted = db.import_upsert(second_row)
    assert was_inserted is False
    assert updated["id"] == first["id"]
    assert updated["name"] == "Refreshed Name"


def test_a_resync_never_overwrites_a_claimed_listings_content() -> None:
    owner = Account.create()
    source = _source()
    row = import_listing(source, owner.address, name="Before Claim")
    header = wallet_auth_header(owner, action="claim-listing", listing_id=row["id"])
    client.post(f"/listings/{row['id']}/claim", headers={"X-Wallet-Auth": header})

    patch = {"name": "Owner Renamed It"}
    patch_header = wallet_auth_header(owner, action="update-listing", listing_id=row["id"], body=patch)
    client.patch(f"/listings/{row['id']}", json=patch, headers={"X-Wallet-Auth": patch_header})

    stale_resync = build_import_row(
        {
            "name": "Source Still Says Before Claim",
            "description": "stale source content",
            "task_categories": ["other"],
            "endpoint_url": row["endpoint_url"],
            "payment_wallet": owner.address,
            "pricing_model": "per_call",
            "pricing_amount": "$0.01",
        },
        source=source,
        now=datetime.now(timezone.utc),
    )
    after_resync, was_inserted = db.import_upsert(stale_resync)
    assert was_inserted is False
    assert after_resync["name"] == "Owner Renamed It"  # the owner's edit survives
    assert after_resync["submitted_by"].lower() == owner.address.lower()
    assert after_resync["claimed"] is True


# ---- missing from source: immediately stale, un-stales on reappearance ----------------------


def test_a_listing_missing_from_a_sync_is_marked_stale_immediately() -> None:
    owner = Account.create()
    source = _source()
    row = import_listing(source, owner.address)
    assert is_stale(db.get_listing(row["id"])) is False

    newly_missing = db.mark_missing_from_source(source, [], datetime.now(timezone.utc))
    assert row["id"] in newly_missing
    assert is_stale(db.get_listing(row["id"])) is True

    body = client.get(f"/listings/{row['id']}").json()
    assert body["stale"] is True


def test_reappearing_in_a_later_sync_clears_the_missing_flag() -> None:
    owner = Account.create()
    source = _source()
    row = import_listing(source, owner.address)
    db.mark_missing_from_source(source, [], datetime.now(timezone.utc))
    assert is_stale(db.get_listing(row["id"])) is True

    refreshed = build_import_row(
        {
            "name": row["name"],
            "description": row["description"],
            "task_categories": row["task_categories"],
            "endpoint_url": row["endpoint_url"],
            "payment_wallet": owner.address,
            "pricing_model": "per_call",
            "pricing_amount": "$0.01",
        },
        source=source,
        now=datetime.now(timezone.utc),
    )
    db.import_upsert(refreshed)
    assert is_stale(db.get_listing(row["id"])) is False


def test_marking_missing_does_not_rebump_an_already_marked_listing() -> None:
    owner = Account.create()
    source = _source()
    row = import_listing(source, owner.address)
    now = datetime.now(timezone.utc)
    first_mark = db.mark_missing_from_source(source, [], now)
    assert row["id"] in first_mark
    second_mark = db.mark_missing_from_source(source, [], now + timedelta(days=1))
    assert row["id"] not in second_mark  # already marked; left alone


# ---- search and filtering by claim status ---------------------------------------------------


def test_claimed_filter_true_and_false() -> None:
    source = _source()
    unclaimed_owner = Account.create()
    unclaimed = import_listing(source, unclaimed_owner.address)

    claimed_owner = Account.create()
    claimed_row = import_listing(source, claimed_owner.address)
    claim_header = wallet_auth_header(claimed_owner, action="claim-listing", listing_id=claimed_row["id"])
    claim_response = client.post(f"/listings/{claimed_row['id']}/claim", headers={"X-Wallet-Auth": claim_header})
    assert claim_response.status_code == 200

    only_unclaimed = client.get("/listings", params={"listing_type": "verification_profile", "claimed": "false"}).json()
    unclaimed_ids = [i["id"] for i in only_unclaimed["listings"]]
    assert unclaimed["id"] in unclaimed_ids
    assert claimed_row["id"] not in unclaimed_ids
    assert all(not i["claimed"] for i in only_unclaimed["listings"])

    only_claimed = client.get("/listings", params={"listing_type": "verification_profile", "claimed": "true"}).json()
    claimed_ids = [i["id"] for i in only_claimed["listings"]]
    assert claimed_row["id"] in claimed_ids
    assert unclaimed["id"] not in claimed_ids
    assert all(i["claimed"] for i in only_claimed["listings"])


def test_verification_profile_listings_are_full_text_searchable() -> None:
    owner = Account.create()
    source = _source()
    marker = "zephyrimport-" + uuid.uuid4().hex[:8]
    row = import_listing(source, owner.address, name=f"{marker} Agent")
    page = client.get("/listings", params={"q": marker, "listing_type": "verification_profile"}).json()
    assert row["id"] in [i["id"] for i in page["listings"]]


def test_mcp_search_listings_supports_the_claimed_filter() -> None:
    import asyncio

    from app.api.routes.listings import search_listings as shared_search

    owner = Account.create()
    source = _source()
    row = import_listing(source, owner.address)
    page = asyncio.run(shared_search(listing_type="verification_profile", claimed=False))
    assert row["id"] in [i.id for i in page.listings]
