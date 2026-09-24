"""Newest-last-activity-first ordering, stable cursor pagination, and the computed
`stale` flag."""

import inspect
import uuid
from datetime import datetime, timezone

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app.core import activity, db
from app.core.pagination import decode_cursor, encode_cursor
from app.main import app
from tests.helpers import assert_error, listing_payload, set_listing_times, wallet_auth_header

client = TestClient(app)


def _make(marker: str, n: int = 1, owner=None) -> list[dict]:
    owner = owner or Account.create()
    out = []
    for i in range(n):
        response = client.post("/listings", json=listing_payload("offering", owner.address, name=f"{marker} #{i}"))
        assert response.status_code == 201, response.text
        out.append(response.json())
    return out


def _search(marker: str, **params) -> dict:
    response = client.get("/listings", params={"q": marker, **params})
    assert response.status_code == 200, response.text
    return response.json()


def _ids(page: dict) -> list[str]:
    return [item["id"] for item in page["listings"]]


# ---- ordering ---------------------------------------------------------------------------


def test_default_order_is_newest_last_activity_first() -> None:
    marker = "order-" + uuid.uuid4().hex[:8]
    a, b, c = _make(marker, 3)
    set_listing_times(a["id"], created_days_ago=10, updated_days_ago=10)
    set_listing_times(b["id"], created_days_ago=5, updated_days_ago=5)
    set_listing_times(c["id"], created_days_ago=1, updated_days_ago=1)
    assert _ids(_search(marker)) == [c["id"], b["id"], a["id"]]


def test_a_heartbeat_moves_a_listing_to_the_top() -> None:
    marker = "beat-" + uuid.uuid4().hex[:8]
    owner = Account.create()
    a, b = _make(marker, 2, owner=owner)
    set_listing_times(a["id"], created_days_ago=20, updated_days_ago=20)
    set_listing_times(b["id"], created_days_ago=2, updated_days_ago=2)
    assert _ids(_search(marker)) == [b["id"], a["id"]]

    header = wallet_auth_header(owner, action="heartbeat-listing", listing_id=a["id"])
    assert client.post(f"/listings/{a['id']}/heartbeat", headers={"X-Wallet-Auth": header}).status_code == 200
    assert _ids(_search(marker)) == [a["id"], b["id"]]


def test_an_edit_moves_a_listing_to_the_top() -> None:
    marker = "edit-" + uuid.uuid4().hex[:8]
    owner = Account.create()
    a, b = _make(marker, 2, owner=owner)
    set_listing_times(a["id"], created_days_ago=20, updated_days_ago=20)
    set_listing_times(b["id"], created_days_ago=2, updated_days_ago=2)

    patch = {"description": "edited just now"}
    header = wallet_auth_header(owner, action="update-listing", listing_id=a["id"], body=patch)
    assert client.patch(f"/listings/{a['id']}", json=patch, headers={"X-Wallet-Auth": header}).status_code == 200
    assert _ids(_search(marker)) == [a["id"], b["id"]]


def test_last_activity_at_is_the_latest_of_the_three_timestamps() -> None:
    marker = "latest-" + uuid.uuid4().hex[:8]
    (listing,) = _make(marker)
    set_listing_times(listing["id"], created_days_ago=30, updated_days_ago=20, last_seen_hours_ago=48)
    got = client.get(f"/listings/{listing['id']}").json()
    seen = datetime.fromisoformat(got["last_seen_at"])
    assert datetime.fromisoformat(got["last_activity_at"]) == seen  # 2 days ago beats 20 and 30

    set_listing_times(listing["id"], last_seen_hours_ago=None)
    got = client.get(f"/listings/{listing['id']}").json()
    assert got["last_seen_at"] is None
    assert datetime.fromisoformat(got["last_activity_at"]) == datetime.fromisoformat(got["updated_at"])


def test_equal_activity_is_ordered_by_id_descending() -> None:
    marker = "tie-" + uuid.uuid4().hex[:8]
    listings = _make(marker, 4)
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with db._connection() as conn:
        for item in listings:
            conn.execute(
                "UPDATE listings SET created_at = %s, updated_at = %s, last_seen_at = NULL WHERE id = %s",
                (when, when, item["id"]),
            )
    assert _ids(_search(marker)) == sorted((i["id"] for i in listings), reverse=True)


# ---- cursor pagination ---------------------------------------------------------------------


def _walk(marker: str, limit: int, between_pages=None) -> list[str]:
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": limit, **({"cursor": cursor} if cursor else {})}
        page = _search(marker, **params)
        seen += _ids(page)
        pages += 1
        assert pages < 50
        cursor = page["next_cursor"]
        if cursor is None:
            return seen
        if between_pages:
            between_pages(pages)


def test_cursor_pagination_visits_every_listing_exactly_once() -> None:
    marker = "cursor-" + uuid.uuid4().hex[:8]
    created = _make(marker, 7)
    walked = _walk(marker, limit=3)
    assert sorted(walked) == sorted(i["id"] for i in created)
    assert len(walked) == len(set(walked))
    assert walked == _ids(_search(marker, limit=100))  # same order as one big page


def test_the_last_page_has_no_next_cursor_and_a_full_page_can_still_be_last() -> None:
    marker = "last-" + uuid.uuid4().hex[:8]
    _make(marker, 4)
    assert _search(marker, limit=4)["next_cursor"] is None
    assert _search(marker, limit=3)["next_cursor"] is not None
    assert _search(marker, limit=100)["next_cursor"] is None


def test_new_listings_added_mid_walk_do_not_cause_skips_or_repeats() -> None:
    marker = "stable-" + uuid.uuid4().hex[:8]
    original = {i["id"] for i in _make(marker, 8)}
    intruders = []

    def add_listing(page_number: int) -> None:
        intruders.extend(i["id"] for i in _make(marker, 2))

    walked = _walk(marker, limit=3, between_pages=add_listing)
    assert intruders, "the test must actually add listings mid-walk"
    assert len(walked) == len(set(walked))  # nothing repeated
    # Every original listing seen exactly once, and the newcomers - which sort ahead of
    # the cursor - never leak into later pages. (Offset paging would repeat items here.)
    assert set(walked) == original


def test_offset_still_works_and_also_returns_a_cursor() -> None:
    marker = "offset-" + uuid.uuid4().hex[:8]
    created = _make(marker, 5)
    page = _search(marker, limit=2, offset=2)
    assert page["offset"] == 2 and len(page["listings"]) == 2
    assert page["next_cursor"] is not None
    assert page["total"] == 5
    assert set(_ids(page)) <= {i["id"] for i in created}


def test_cursor_with_an_offset_is_rejected() -> None:
    marker = "both-" + uuid.uuid4().hex[:8]
    _make(marker, 3)
    cursor = _search(marker, limit=1)["next_cursor"]
    response = client.get("/listings", params={"q": marker, "cursor": cursor, "offset": 1})
    assert_error(response, 422, "invalid_pagination")


@pytest.mark.parametrize("bad", ["not-a-cursor", "e30", "%%%%", "eyJhIjoibm9wZSIsImkiOiJ4In0"])
def test_a_malformed_cursor_is_rejected_with_a_way_to_restart(bad: str) -> None:
    body = assert_error(client.get("/listings", params={"cursor": bad}), 422, "invalid_cursor")
    assert body["next_actions"][0]["path"] == "/listings"


def test_an_oversized_cursor_is_rejected() -> None:
    assert client.get("/listings", params={"cursor": "A" * 600}).status_code == 422


def test_cursor_round_trip_and_timezone_requirement() -> None:
    when = datetime(2026, 5, 1, 12, 30, 45, 123456, tzinfo=timezone.utc)
    assert decode_cursor(encode_cursor(when, "abc")) == (when, "abc")
    naive = encode_cursor(datetime(2026, 5, 1), "abc")
    with pytest.raises(Exception):
        decode_cursor(naive)


# ---- stale ----------------------------------------------------------------------------------


def test_a_new_listing_is_not_stale() -> None:
    (listing,) = _make("fresh-" + uuid.uuid4().hex[:8])
    assert listing["stale"] is False


def test_stale_flips_after_the_threshold() -> None:
    (listing,) = _make("stale-" + uuid.uuid4().hex[:8])
    for days, expected in ((59, False), (61, True)):
        set_listing_times(listing["id"], created_days_ago=days, updated_days_ago=days, last_seen_hours_ago=None)
        assert client.get(f"/listings/{listing['id']}").json()["stale"] is expected


def test_any_recent_activity_clears_stale() -> None:
    owner = Account.create()
    (listing,) = _make("unstale-" + uuid.uuid4().hex[:8], owner=owner)
    set_listing_times(listing["id"], created_days_ago=200, updated_days_ago=200, last_seen_hours_ago=None)
    assert client.get(f"/listings/{listing['id']}").json()["stale"] is True

    header = wallet_auth_header(owner, action="heartbeat-listing", listing_id=listing["id"])
    response = client.post(f"/listings/{listing['id']}/heartbeat", headers={"X-Wallet-Auth": header})
    assert response.json()["stale"] is False
    assert client.get(f"/listings/{listing['id']}").json()["stale"] is False


def test_the_threshold_is_configurable(monkeypatch) -> None:
    (listing,) = _make("config-" + uuid.uuid4().hex[:8])
    set_listing_times(listing["id"], created_days_ago=15, updated_days_ago=15, last_seen_hours_ago=None)
    assert client.get(f"/listings/{listing['id']}").json()["stale"] is False
    monkeypatch.setattr(activity, "STALE_AFTER_DAYS", 10.0)
    assert client.get(f"/listings/{listing['id']}").json()["stale"] is True


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "99999", ""])
def test_a_bad_threshold_fails_loudly_at_startup(monkeypatch, bad: str) -> None:
    monkeypatch.setenv("STALE_AFTER_DAYS", bad)
    with pytest.raises(RuntimeError):
        activity._stale_after_days()


def test_the_threshold_defaults_to_sixty_days_and_accepts_overrides(monkeypatch) -> None:
    monkeypatch.delenv("STALE_AFTER_DAYS", raising=False)
    assert activity._stale_after_days() == 60.0
    monkeypatch.setenv("STALE_AFTER_DAYS", "30")
    assert activity._stale_after_days() == 30.0


def test_staleness_is_computed_from_stored_data_with_no_outbound_calls() -> None:
    source = inspect.getsource(activity)
    for banned in ("httpx", "requests", "urllib.request", "socket", "aiohttp"):
        assert banned not in source
