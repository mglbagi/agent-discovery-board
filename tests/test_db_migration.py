"""The schema migration: a `listings` table created by the previous version (no
last_seen_at / payment_options / endpoint_key, no duplicate guard) is upgraded in place
when the service next starts - existing rows keep working and get backfilled."""

import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.core import db
from app.main import app

LEGACY_DDL = """
CREATE TABLE listings (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    listing_type TEXT NOT NULL,
    task_categories TEXT[] NOT NULL,
    endpoint_url TEXT NOT NULL,
    payment_wallet TEXT NOT NULL,
    pricing_model TEXT,
    pricing_amount TEXT,
    erc8004_identity TEXT,
    verification_agent_id TEXT,
    submitted_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
)
"""
OWNER = "0x42a3c399f83BCcC3b9eEf81e65954a715D54855E"


def _insert_legacy(
    conn, endpoint_url: str, *, status: str = "active", submitted_by: str = OWNER, listing_type: str = "offering",
    name: str = "Legacy",
) -> str:
    listing_id = str(uuid.uuid4())
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO listings (id, name, description, listing_type, task_categories, endpoint_url, payment_wallet, "
        "pricing_model, pricing_amount, submitted_by, status, created_at, updated_at) "
        "VALUES (%s, %s, 'made by the previous version', %s, %s, %s, %s, 'per_call', '$0.02', %s, %s, %s, %s)",
        (listing_id, name, listing_type, ["data validation"], endpoint_url, OWNER, submitted_by, status, now, now),
    )
    return listing_id


@pytest.fixture
def legacy_table():
    db.close_db()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS listings")
        conn.execute(LEGACY_DDL)
        yield conn
        conn.execute("DROP TABLE IF EXISTS listings")
    db.close_db()  # the next test re-creates the current schema


def test_a_legacy_table_is_upgraded_in_place_and_rows_are_backfilled(legacy_table) -> None:
    listing_id = _insert_legacy(legacy_table, "https://Example.COM:443/verify/schema/")

    db.init_db()

    columns = {r[0] for r in legacy_table.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'listings'"
    ).fetchall()}
    assert {"last_seen_at", "payment_options", "endpoint_key"} <= columns
    row = legacy_table.execute(
        "SELECT endpoint_key, payment_options, last_seen_at FROM listings WHERE id = %s", (listing_id,)
    ).fetchone()
    assert row == ("https://example.com/verify/schema", [], None)
    indexes = {r[0] for r in legacy_table.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'listings'").fetchall()}
    assert {"listings_active_offering_scope_endpoint_owner_uniq", "listings_activity_idx"} <= indexes


def test_the_migrated_row_is_served_normally_and_the_duplicate_guard_covers_it(legacy_table) -> None:
    listing_id = _insert_legacy(legacy_table, "https://legacy.example.com/agent")
    db.init_db()
    client = TestClient(app)

    body = client.get(f"/listings/{listing_id}").json()
    assert body["payment_options"] == [] and body["last_seen_at"] is None and body["stale"] is False
    assert [item["id"] for item in client.get("/listings").json()["listings"]] == [listing_id]

    duplicate = {
        "name": "Again", "description": "same endpoint, same owner", "listing_type": "offering",
        "task_categories": ["other"], "endpoint_url": "https://LEGACY.example.com/agent/",
        "payment_wallet": OWNER, "submitted_by": OWNER,
    }
    response = client.post("/listings", json=duplicate)
    assert response.status_code == 409 and response.json()["existing_listing_id"] == listing_id


def test_migrating_twice_is_harmless(legacy_table) -> None:
    listing_id = _insert_legacy(legacy_table, "https://twice.example.com/agent")
    db.init_db()
    db.close_db()
    db.init_db()
    assert db.get_listing(listing_id)["id"] == listing_id


def test_legacy_duplicate_offerings_make_startup_fail_loudly_instead_of_guessing(legacy_table) -> None:
    _insert_legacy(legacy_table, "https://dup.example.com/agent")
    _insert_legacy(legacy_table, "https://DUP.example.com/agent/")  # same service, both active
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.init_db()


def test_legacy_duplicates_are_fine_once_one_is_inactive(legacy_table) -> None:
    _insert_legacy(legacy_table, "https://dup2.example.com/agent")
    _insert_legacy(legacy_table, "https://dup2.example.com/agent", status="inactive")
    db.init_db()  # only ACTIVE duplicates are forbidden


@pytest.mark.parametrize("listing_type", ["announcement", "notice", "request"])
def test_legacy_repeats_of_non_offering_types_migrate_fine(legacy_table, listing_type: str) -> None:
    for _ in range(3):
        _insert_legacy(legacy_table, "https://repeat.example.com/agent", listing_type=listing_type)
    db.init_db()  # the guard covers offerings only


def test_an_offering_and_announcements_about_it_coexist_in_legacy_data(legacy_table) -> None:
    _insert_legacy(legacy_table, "https://mix.example.com/agent")
    _insert_legacy(legacy_table, "https://mix.example.com/agent", listing_type="announcement")
    _insert_legacy(legacy_table, "https://mix.example.com/agent", listing_type="announcement")
    db.init_db()


def test_the_earlier_all_types_index_is_replaced(legacy_table) -> None:
    legacy_table.execute("ALTER TABLE listings ADD COLUMN endpoint_key TEXT")
    legacy_table.execute(
        "CREATE UNIQUE INDEX listings_active_endpoint_owner_uniq ON listings (endpoint_key, lower(submitted_by)) "
        "WHERE status = 'active'"
    )
    db.init_db()
    indexes = {r[0] for r in legacy_table.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'listings'").fetchall()}
    assert "listings_active_endpoint_owner_uniq" not in indexes
    assert "listings_active_offering_scope_endpoint_owner_uniq" in indexes


def test_existing_rows_migrate_as_real_even_when_named_test(legacy_table) -> None:
    listing_id = _insert_legacy(legacy_table, "https://named-test.example.com/agent", name="test-runner service")
    legacy_table.execute("UPDATE listings SET created_at = '2020-01-01T00:00:00Z' WHERE id = %s", (listing_id,))
    db.init_db()

    assert legacy_table.execute("SELECT is_test FROM listings WHERE id = %s", (listing_id,)).fetchone() == (False,)
    from datetime import datetime, timezone

    assert db.purge_expired_test_listings(datetime.now(timezone.utc), 100) == []  # never purged
    assert db.get_listing(listing_id)["name"] == "test-runner service"


def test_the_previous_versions_duplicate_index_is_replaced_by_the_scoped_one(legacy_table) -> None:
    legacy_table.execute("ALTER TABLE listings ADD COLUMN endpoint_key TEXT")
    legacy_table.execute(
        "CREATE UNIQUE INDEX listings_active_offering_endpoint_owner_uniq ON listings (endpoint_key, lower(submitted_by)) "
        "WHERE status = 'active' AND listing_type = 'offering'"
    )
    db.init_db()
    indexes = {r[0] for r in legacy_table.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'listings'").fetchall()}
    assert "listings_active_offering_endpoint_owner_uniq" not in indexes
    assert {"listings_active_offering_scope_endpoint_owner_uniq", "listings_test_created_idx"} <= indexes
