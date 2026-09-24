"""`test-` listings: temporary demo data - stored and usable by id, hidden from default
browse/search and the MCP tool, isolated from real duplicate detection, never given a
badge lookup, and purged lazily (startup + throttled during requests), never on a timer."""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app import main as main_module
from app.core import db, demo_data, maintenance, score_client
from app.core.errors import ERROR_CODES
from app.main import app
from tests.helpers import assert_error, listing_payload, set_listing_times, wallet_auth_header

client = TestClient(app)


def _wipe_test_rows() -> None:
    db.purge_expired_test_listings(datetime.now(timezone.utc) + timedelta(days=3650), 100_000)


@pytest.fixture(autouse=True)
def _isolated_test_rows():
    _wipe_test_rows()
    maintenance._last_run = None
    yield
    _wipe_test_rows()
    maintenance._last_run = None


def _make(name: str, listing_type: str = "offering", owner=None, **overrides) -> dict:
    owner = owner or Account.create()
    response = client.post("/listings", json=listing_payload(listing_type, owner.address, name=name, **overrides))
    assert response.status_code == 201, response.text
    body = response.json()
    body["_owner"] = owner
    return body


def _ids(response) -> list[str]:
    return [item["id"] for item in response.json()["listings"]]


def _tname() -> str:
    return f"test-{uuid.uuid4().hex[:10]}"


# ---- what makes a listing a test listing ----------------------------------------------------


def test_a_test_prefixed_name_creates_a_test_listing_with_an_expiry() -> None:
    listing = _make(_tname())
    assert listing["test"] is True
    created = datetime.fromisoformat(listing["created_at"])
    assert datetime.fromisoformat(listing["expires_at"]) == created + timedelta(hours=demo_data.TEST_LISTING_TTL_HOURS)


def test_real_listings_are_not_test_listings_and_never_expire() -> None:
    listing = _make("A perfectly ordinary listing " + uuid.uuid4().hex[:6])
    assert listing["test"] is False and listing["expires_at"] is None


@pytest.mark.parametrize("name", ["Test-x", "TEST-x", "my-test-x", "test_x", "testing-x", " test-x", "xtest-"])
def test_only_the_exact_lowercase_prefix_counts(name: str) -> None:
    assert _make(f"{name}{uuid.uuid4().hex[:6]}")["test"] is False


def test_the_flag_is_decided_at_creation_and_returned_everywhere() -> None:
    listing = _make(_tname())
    assert client.get(f"/listings/{listing['id']}").json()["test"] is True
    page = client.get("/listings", params={"include_test": "true", "q": listing["name"]}).json()
    assert page["listings"][0]["test"] is True and page["listings"][0]["expires_at"] == listing["expires_at"]


# ---- hidden from browse and search, usable by id -----------------------------------------------


def test_test_listings_are_hidden_from_default_browse_and_search() -> None:
    test_listing = _make(_tname())
    real = _make("real-" + uuid.uuid4().hex[:8])
    default = client.get("/listings", params={"limit": 100})
    assert test_listing["id"] not in _ids(default) and real["id"] in _ids(default)
    by_text = client.get("/listings", params={"q": test_listing["name"]}).json()
    assert by_text["total"] == 0 and by_text["listings"] == []


def test_include_test_reveals_them_and_the_total_agrees() -> None:
    a, b = _make(_tname()), _make(_tname())
    real = _make("real-" + uuid.uuid4().hex[:8])
    shown = client.get("/listings", params={"include_test": "true", "limit": 100}).json()
    assert {a["id"], b["id"], real["id"]} <= {i["id"] for i in shown["listings"]}
    hidden_total = client.get("/listings", params={"limit": 1}).json()["total"]
    assert shown["total"] == hidden_total + 2


def test_filters_and_cursors_work_with_include_test() -> None:
    marker = uuid.uuid4().hex[:8]
    created = [_make(f"test-{marker}-{i}", task_categories=["translation"]) for i in range(5)]
    params = {"include_test": "true", "q": marker, "task_category": "translation", "listing_type": "offering", "limit": 2}
    seen, cursor = [], None
    for _ in range(10):
        page = client.get("/listings", params={**params, **({"cursor": cursor} if cursor else {})}).json()
        seen += [i["id"] for i in page["listings"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(c["id"] for c in created)


def test_a_test_listing_works_by_id_for_get_patch_heartbeat_and_delete() -> None:
    listing = _make(_tname())
    owner, lid = listing["_owner"], listing["id"]
    assert client.get(f"/listings/{lid}").status_code == 200

    patch = {"description": "still usable by id"}
    header = wallet_auth_header(owner, action="update-listing", listing_id=lid, body=patch)
    assert client.patch(f"/listings/{lid}", json=patch, headers={"X-Wallet-Auth": header}).status_code == 200

    header = wallet_auth_header(owner, action="heartbeat-listing", listing_id=lid)
    assert client.post(f"/listings/{lid}/heartbeat", headers={"X-Wallet-Auth": header}).status_code == 200

    header = wallet_auth_header(owner, action="delete-listing", listing_id=lid)
    deactivated = client.delete(f"/listings/{lid}", headers={"X-Wallet-Auth": header})
    assert deactivated.status_code == 200 and deactivated.json()["status"] == "inactive"
    assert deactivated.json()["test"] is True


# ---- duplicate detection: separate scopes ---------------------------------------------------------


def test_a_test_offering_and_a_real_offering_do_not_collide() -> None:
    owner = Account.create()
    url = f"https://scope-{uuid.uuid4().hex[:8]}.example.com/x"
    test_first = _make(_tname(), owner=owner, endpoint_url=url)
    real = _make("real-" + uuid.uuid4().hex[:6], owner=owner, endpoint_url=url)  # allowed alongside the test one
    assert test_first["id"] != real["id"]

    body = assert_error(
        client.post("/listings", json=listing_payload("offering", owner.address, name="real-again", endpoint_url=url)),
        409,
        "duplicate_listing",
    )
    assert body["existing_listing_id"] == real["id"]  # never the test listing


def test_a_real_offering_is_not_blocked_by_an_earlier_test_offering() -> None:
    owner = Account.create()
    url = f"https://first-{uuid.uuid4().hex[:8]}.example.com/x"
    _make(_tname(), owner=owner, endpoint_url=url)
    assert client.post("/listings", json=listing_payload("offering", owner.address, name="real-one", endpoint_url=url)).status_code == 201


def test_test_offerings_still_collide_with_each_other_so_a_demo_can_show_a_409() -> None:
    owner = Account.create()
    url = f"https://demo-{uuid.uuid4().hex[:8]}.example.invalid/x"
    first = _make(_tname(), owner=owner, endpoint_url=url)
    body = assert_error(
        client.post("/listings", json=listing_payload("offering", owner.address, name=_tname(), endpoint_url=url)),
        409,
        "duplicate_listing",
    )
    assert body["existing_listing_id"] == first["id"] and "test listings" in body["message"]


def test_the_database_index_is_scoped_by_is_test() -> None:
    def row(name: str, is_test: bool) -> dict:
        from app.core.models import ListingCreate

        now = datetime.now(timezone.utc)
        model = ListingCreate(**listing_payload("offering", "0x" + "cd" * 20, name=name, endpoint_url="https://idx.example.com/a"))
        return {**model.model_dump(), "id": str(uuid.uuid4()), "status": "active", "is_test": is_test, "created_at": now, "updated_at": now}

    db.create_listing(row("real", False))
    db.create_listing(row("test-idx-1", True))  # different scope: fine
    with pytest.raises(db.UniqueViolation):
        db.create_listing(row("test-idx-2", True))
    with pytest.raises(db.UniqueViolation):
        db.create_listing(row("real-2", False))


# ---- the prefix cannot be added or removed later -----------------------------------------------------


def test_a_real_listing_cannot_be_renamed_into_a_test_listing() -> None:
    real = _make("real-" + uuid.uuid4().hex[:8])
    patch = {"name": _tname()}
    header = wallet_auth_header(real["_owner"], action="update-listing", listing_id=real["id"], body=patch)
    response = client.patch(f"/listings/{real['id']}", json=patch, headers={"X-Wallet-Auth": header})
    body = assert_error(response, 422, "invalid_test_name")
    assert body["next_actions"][0]["required_fields"] == ["header:X-Wallet-Auth", "name"]
    assert client.get(f"/listings/{real['id']}").json()["name"] == real["name"]


def test_a_test_listing_cannot_shed_the_prefix() -> None:
    listing = _make(_tname())
    patch = {"name": "now a real-looking name"}
    header = wallet_auth_header(listing["_owner"], action="update-listing", listing_id=listing["id"], body=patch)
    response = client.patch(f"/listings/{listing['id']}", json=patch, headers={"X-Wallet-Auth": header})
    assert_error(response, 422, "invalid_test_name")


def test_renames_that_keep_the_status_are_fine() -> None:
    listing = _make(_tname())
    real = _make("real-" + uuid.uuid4().hex[:8])
    for item, new_name in ((listing, _tname()), (real, "real-renamed-" + uuid.uuid4().hex[:6])):
        patch = {"name": new_name}
        header = wallet_auth_header(item["_owner"], action="update-listing", listing_id=item["id"], body=patch)
        response = client.patch(f"/listings/{item['id']}", json=patch, headers={"X-Wallet-Auth": header})
        assert response.status_code == 200 and response.json()["test"] is item["test"]


def test_patching_other_fields_never_trips_the_name_rule() -> None:
    listing = _make(_tname())
    patch = {"description": "just a description"}
    header = wallet_auth_header(listing["_owner"], action="update-listing", listing_id=listing["id"], body=patch)
    assert client.patch(f"/listings/{listing['id']}", json=patch, headers={"X-Wallet-Auth": header}).status_code == 200


# ---- no badge lookups ------------------------------------------------------------------------------------


def test_test_listings_never_cost_a_badge_lookup(monkeypatch) -> None:
    lookups: list[str] = []

    async def fake_fetch(agent_id):
        lookups.append(agent_id)
        return {"trust_score": 0.5, "confidence_interval_95": None, "sample_size": 1, "identity_verified": False,
                "reason": None, "fetched_at": "now", "source": "test"}

    monkeypatch.setattr(score_client, "_PAYER_KEY", "0x" + "11" * 32)
    monkeypatch.setattr(score_client, "_fetch", fake_fetch)
    score_client.reset_cache()
    try:
        test_listing = _make(_tname())
        assert client.get(f"/listings/{test_listing['id']}").json()["badge"] is None
        assert lookups == []

        real = _make("real-" + uuid.uuid4().hex[:8])
        assert client.get(f"/listings/{real['id']}").json()["badge"] is not None
        assert len(lookups) == 1
    finally:
        score_client.reset_cache()


# ---- purge: bounded, only test rows, lazy ------------------------------------------------------------------


def test_purge_removes_only_expired_test_listings() -> None:
    expired = _make(_tname())
    fresh = _make(_tname())
    old_real = _make("old real " + uuid.uuid4().hex[:6])
    set_listing_times(expired["id"], created_days_ago=2)
    set_listing_times(old_real["id"], created_days_ago=4000)

    removed = maintenance.purge_expired_test_listings()

    assert removed == [expired["id"]]
    assert db.get_listing(expired["id"]) is None
    assert db.get_listing(fresh["id"]) is not None
    assert db.get_listing(old_real["id"]) is not None  # ancient, but real: untouchable


def test_purge_is_bounded_and_drains_oldest_first(monkeypatch) -> None:
    items = [_make(_tname()) for _ in range(5)]
    for age, item in zip((10, 9, 8, 7, 6), items):
        set_listing_times(item["id"], created_days_ago=age)
    monkeypatch.setattr(demo_data, "PURGE_BATCH_SIZE", 2)

    # Which rows go is decided by the subquery's ORDER BY created_at (oldest first); the order of
    # DELETE ... RETURNING itself is unspecified, so compare batches as sets.
    assert set(maintenance.purge_expired_test_listings()) == {items[0]["id"], items[1]["id"]}
    assert set(maintenance.purge_expired_test_listings()) == {items[2]["id"], items[3]["id"]}
    assert maintenance.purge_expired_test_listings() == [items[4]["id"]]
    assert maintenance.purge_expired_test_listings() == []


def test_a_request_opportunistically_purges_and_the_purge_is_throttled(monkeypatch) -> None:
    monkeypatch.setattr(demo_data, "PURGE_MIN_INTERVAL_SECONDS", 10_000.0)
    first = _make(_tname())
    set_listing_times(first["id"], created_days_ago=3)
    maintenance._last_run = None

    client.get("/listings")  # a normal browse request
    assert db.get_listing(first["id"]) is None  # purged on the way through

    second = _make(_tname())
    set_listing_times(second["id"], created_days_ago=3)
    client.get("/listings")
    assert db.get_listing(second["id"]) is not None  # throttled: not yet due again

    maintenance._last_run = None
    client.get("/listings")
    assert db.get_listing(second["id"]) is None


def test_creating_a_listing_also_triggers_the_purge() -> None:
    stale = _make(_tname())
    set_listing_times(stale["id"], created_days_ago=3)
    maintenance._last_run = None
    _make("real-" + uuid.uuid4().hex[:8])
    assert db.get_listing(stale["id"]) is None


def test_the_search_tool_function_triggers_it_too() -> None:
    from app.api.routes.listings import search_listings

    stale = _make(_tname())
    set_listing_times(stale["id"], created_days_ago=3)
    maintenance._last_run = None
    asyncio.run(search_listings(limit=1))
    assert db.get_listing(stale["id"]) is None


def test_the_throttle_costs_nothing_when_not_due(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(db, "purge_expired_test_listings", lambda cutoff, limit: calls.append(1) or [])
    maintenance._last_run = None
    for _ in range(50):
        maintenance.maybe_purge()
    assert len(calls) == 1


def test_a_failing_purge_never_fails_the_request(monkeypatch, caplog) -> None:
    def boom(cutoff, limit):
        raise RuntimeError("purge exploded")

    monkeypatch.setattr(db, "purge_expired_test_listings", boom)
    maintenance._last_run = None
    with caplog.at_level(logging.ERROR, logger="app.maintenance"):
        response = client.get("/listings")
    assert response.status_code == 200
    assert "opportunistic test-listing purge failed" in caplog.text


def test_startup_maintenance_purges_expired_listings() -> None:
    expired = _make(_tname())
    fresh = _make(_tname())
    set_listing_times(expired["id"], created_days_ago=3)
    maintenance.startup_maintenance()
    assert db.get_listing(expired["id"]) is None and db.get_listing(fresh["id"]) is not None
    assert maintenance._last_run is not None  # the request-time throttle starts from here


def test_startup_maintenance_never_raises(monkeypatch, caplog) -> None:
    def boom(*a, **k):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(db, "init_db", boom)
    maintenance.startup_maintenance()  # swallowed: the service must still boot

    monkeypatch.undo()
    monkeypatch.setattr(db, "purge_expired_test_listings", boom)
    with caplog.at_level(logging.ERROR):
        maintenance.startup_maintenance()


def test_the_real_lifespan_runs_the_startup_purge(monkeypatch) -> None:
    expired = _make(_tname())
    set_listing_times(expired["id"], created_days_ago=3)

    @asynccontextmanager
    async def fake_run():
        yield

    # The MCP session manager may only be entered once per process; stand it in so the
    # app's own lifespan (which is what production runs) can be exercised here.
    monkeypatch.setattr(main_module, "mcp_server", SimpleNamespace(session_manager=SimpleNamespace(run=fake_run)))

    async def boot():
        async with main_module.lifespan(main_module.app):
            return db.get_listing(expired["id"])

    assert asyncio.run(boot()) is None


def test_the_expiry_setting_is_validated_and_defaults_to_a_day(monkeypatch) -> None:
    monkeypatch.delenv("TEST_LISTING_TTL_HOURS", raising=False)
    assert demo_data._ttl_hours() == 24.0
    monkeypatch.setenv("TEST_LISTING_TTL_HOURS", "48")
    assert demo_data._ttl_hours() == 48.0
    for bad in ("abc", "0", "-1", "721", ""):
        monkeypatch.setenv("TEST_LISTING_TTL_HOURS", bad)
        with pytest.raises(RuntimeError):
            demo_data._ttl_hours()


def test_the_purge_interval_setting_is_validated(monkeypatch) -> None:
    monkeypatch.setenv("TEST_PURGE_MIN_INTERVAL_SECONDS", "60")
    assert demo_data._purge_interval_seconds() == 60.0
    for bad in ("abc", "0", "0.5", "86401", ""):
        monkeypatch.setenv("TEST_PURGE_MIN_INTERVAL_SECONDS", bad)
        with pytest.raises(RuntimeError):
            demo_data._purge_interval_seconds()


# ---- documentation: agents are told ----------------------------------------------------------------------


def test_the_manifest_documents_test_listings() -> None:
    params = client.get("/.well-known/agent-card.json").json()["capabilities"]["extensions"][0]["params"]
    doc = params["testListings"]
    assert doc["prefix"] == "test-" and doc["ttlHours"] == demo_data.TEST_LISTING_TTL_HOURS
    assert "include_test=true" in doc["description"] and "hidden" in doc["description"]
    assert "example.invalid" in doc["suggestedEndpointUrls"]
    assert "no background timer" in doc["purge"]
    assert doc["responseFields"] == ["test", "expires_at"]
    assert "include_test" in params["endpoints"]["browse"]["queryParams"]
    assert {"test", "expires_at"} <= set(params["outputSchema"]["properties"])


def test_llms_txt_tells_agents_test_listings_are_hidden_and_temporary() -> None:
    text = client.get("/llms.txt").text
    for needle in ("test-", "TEMPORARY", "include_test=true", "example.invalid", "invalid_test_name"):
        assert needle in text, needle
    assert f"{demo_data.TEST_LISTING_TTL_HOURS:g}h" in text


def test_openapi_documents_include_test_and_the_new_error_code() -> None:
    spec = client.get("/openapi.json").json()
    params = {p["name"]: p for p in spec["paths"]["/listings"]["get"]["parameters"]}
    assert params["include_test"]["schema"]["default"] is False
    assert {"test", "expires_at"} <= set(spec["components"]["schemas"]["ListingResponse"]["properties"])
    assert ERROR_CODES["invalid_test_name"].http_status == 422
    assert "`invalid_test_name`" in spec["info"]["description"]
