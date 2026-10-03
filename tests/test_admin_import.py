"""POST /admin/import: the protected endpoint that runs the bulk import script's sync (same
validation, upsert by source + endpoint, claimed listings untouched, do-not-import honored,
missing -> stale), authorized only by IMPORT_API_KEY, with dry_run, a size limit, rate limits
and one counts-only audit line per call. Plus: imported listings' template_url is the board's
own GET /listings/{id}/template."""

import importlib.util
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app.api.routes import admin_import
from app.core import db, import_auth
from app.main import app
from tests.helpers import assert_error, wallet_auth_header

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("bulk_import_listings", ROOT / "scripts" / "bulk_import_listings.py")
bulk_import = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bulk_import)

client = TestClient(app)

KEY = "test-import-key-0123456789abcdef"  # conftest sets it WITH surrounding whitespace
AUTH = {"Authorization": f"Bearer {KEY}"}
BASE_URL = os.environ["SERVICE_BASE_URL"]


def _source() -> str:
    return "pub-" + uuid.uuid4().hex[:8]


def _record(name: str | None = None, **overrides) -> dict:
    base = {
        # One distinctive token: the other import tests assert on fuzzy name searches, and a
        # name like "Published Agent ..." sits close enough to their "Imported Agent ..." to
        # make those fall back to a trigram match.
        "name": f"Zebra{uuid.uuid4().hex[:10]}" if name is None else name,
        "description": "A listing published through the import endpoint.",
        "task_categories": ["other"],
        "endpoint_url": f"https://example.com/pub/{uuid.uuid4().hex}",
        "payment_wallet": Account.create().address,
        "pricing_model": "per_call",
        "pricing_amount": "$0.01",
        "output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
        "template_url": "https://raw.githubusercontent.com/example/repo/main/templates/vt_x.json",
    }
    base.update(overrides)
    return base


def _jsonl(records: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in records) + "\n"


def _post(records, *, headers=AUTH, **params):
    body = records if isinstance(records, (str, bytes)) else _jsonl(records)
    return client.post("/admin/import", params=params, content=body, headers=headers)


def _exists(source: str, record: dict) -> dict[str, bool]:
    return db.existing_import_listings(source, [record["endpoint_url"]])


def _get(source: str, record: dict) -> dict:
    page = client.get("/listings", params={"q": record["name"], "listing_type": "verification_profile"}).json()
    found = [i for i in page["listings"] if i["endpoint_url"] == record["endpoint_url"] and i["source"] == source]
    assert len(found) == 1, page
    return found[0]


@pytest.fixture(autouse=True)
def _fresh_limiters():
    saved = (
        import_auth.import_limiter.max_requests, import_auth.import_auth_failure_limiter.max_requests,
    )
    import_auth.import_limiter.reset()
    import_auth.import_auth_failure_limiter.reset()
    yield
    import_auth.import_limiter.max_requests, import_auth.import_auth_failure_limiter.max_requests = saved
    import_auth.import_limiter.reset()
    import_auth.import_auth_failure_limiter.reset()


@pytest.fixture
def audit_lines():
    lines: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    handler = _Capture()
    import_auth.audit_logger.addHandler(handler)
    yield lines
    import_auth.audit_logger.removeHandler(handler)


def _audit_entries(lines: list[str]) -> list[dict]:
    assert all(line.startswith("[import-audit] ") for line in lines), lines
    return [json.loads(line[len("[import-audit] "):]) for line in lines]


# ---- the key: required at startup, stripped, constant-time, nothing without it ---------------------


def test_startup_fails_loudly_without_the_key(monkeypatch) -> None:
    monkeypatch.delenv("IMPORT_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="IMPORT_API_KEY"):
        import_auth._load_key()
    monkeypatch.setenv("IMPORT_API_KEY", " \t\n ")
    with pytest.raises(RuntimeError, match="IMPORT_API_KEY"):
        import_auth._load_key()


def test_the_app_itself_refuses_to_boot_with_a_blank_key() -> None:
    env = {**os.environ, "IMPORT_API_KEY": "   "}  # set (even if blank), so .env cannot fill it in
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"], env=env, cwd=ROOT, capture_output=True, text=True, timeout=180
    )
    assert result.returncode != 0
    assert "IMPORT_API_KEY" in result.stderr
    assert "test-import-key" not in result.stderr


def test_the_key_is_stripped_of_surrounding_whitespace() -> None:
    assert os.environ["IMPORT_API_KEY"] != KEY  # the env value carries whitespace
    assert import_auth.key_matches(KEY)
    assert import_auth.key_matches(f"  {KEY}\n")  # a padded header value is fine too


def test_the_comparison_is_constant_time(monkeypatch) -> None:
    calls = []
    real = import_auth.hmac.compare_digest

    def spy(a, b):
        calls.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(import_auth.hmac, "compare_digest", spy)
    assert import_auth.key_matches("x") is False
    assert import_auth.key_matches("x" * 500) is False
    assert import_auth.key_matches(KEY) is True
    # always digests of equal length, whatever length the caller sent
    assert calls == [(32, 32)] * 3


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer wrong-key"},
        {"Authorization": f"Bearer {KEY}x"},
        {"Authorization": f"Bearer {KEY[:-1]}"},
        {"Authorization": f"Basic {KEY}"},
        {"Authorization": KEY},
        {"X-Import-Key": KEY},
        {"X-API-Key": KEY},
    ],
)
def test_everything_without_the_right_key_is_rejected_and_writes_nothing(headers) -> None:
    source, record = _source(), _record()
    for params in ({"dry_run": "false", "source": source}, {"dry_run": "true", "source": source}, {}):
        response = _post([record], headers=headers, **params)
        body = assert_error(response, 401, "unauthorized")
        assert response.headers["www-authenticate"] == "Bearer"
        assert KEY not in json.dumps(body)
    assert _exists(source, record) == {}


def test_a_wrong_key_is_rejected_before_the_request_is_looked_at() -> None:
    # not even a malformed parameter or a body that is not JSON is reported to a caller
    # without the key: they only ever learn "unauthorized"
    assert_error(_post("{not json", headers={}, dry_run="banana"), 401, "unauthorized")
    assert_error(_post("{not json", headers={"Authorization": "Bearer nope"}, mark_missing="x"), 401, "unauthorized")


def test_the_endpoint_is_not_advertised_in_the_public_api_documents() -> None:
    assert "/admin/import" not in json.dumps(client.get("/openapi.json").json())
    assert "/admin/import" not in client.get("/.well-known/agent-card.json").text
    assert "/admin/import" not in client.get("/llms.txt").text


# ---- rate limits ----------------------------------------------------------------------------------


def test_wrong_key_attempts_are_rate_limited_but_the_real_key_still_works() -> None:
    import_auth.import_auth_failure_limiter.max_requests = 3
    codes = [_post([_record()], headers={"Authorization": "Bearer nope"}).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    limited = _post([_record()], headers={"Authorization": "Bearer nope"})
    assert_error(limited, 429, "rate_limited")
    assert int(limited.headers["retry-after"]) >= 1
    # the publisher, even from the same address, is not locked out by someone guessing
    assert _post([_record()], source=_source(), dry_run="true").status_code == 200


def test_authorized_calls_are_rate_limited_too(audit_lines) -> None:
    import_auth.import_limiter.max_requests = 2
    assert _post([_record()], dry_run="true", source=_source()).status_code == 200
    assert _post([_record()], dry_run="true", source=_source()).status_code == 200
    third = _post([_record()], dry_run="true", source=_source())
    assert_error(third, 429, "rate_limited")
    assert [e["outcome"] for e in _audit_entries(audit_lines)] == ["dry_run", "dry_run", "rate_limited"]


# ---- size limit -----------------------------------------------------------------------------------


def test_an_oversized_declared_body_is_refused_and_audited(monkeypatch, audit_lines) -> None:
    monkeypatch.setattr(admin_import, "IMPORT_MAX_BODY_BYTES", 400)
    big = _jsonl([_record() for _ in range(5)])
    assert len(big) > 400
    assert_error(_post(big, dry_run="false", source=_source()), 413, "body_too_large")
    entry = _audit_entries(audit_lines)[-1]
    assert (entry["outcome"], entry["status"]) == ("too_large", 413)


def test_an_oversized_streamed_body_is_refused_even_without_a_content_length(monkeypatch) -> None:
    monkeypatch.setattr(admin_import, "IMPORT_MAX_BODY_BYTES", 400)
    line = (json.dumps(_record()) + "\n").encode()

    def chunks():
        for _ in range(6):
            yield line

    source = _source()
    response = client.post("/admin/import", params={"source": source}, content=chunks(), headers=AUTH)
    assert_error(response, 413, "body_too_large")


def test_a_body_beyond_the_global_ceiling_is_refused_before_anything_else() -> None:
    too_big = b"x" * (import_auth.IMPORT_MAX_BODY_BYTES + 1)
    response = client.post("/admin/import", content=too_big, headers={})  # not even authorized
    assert response.status_code == 413
    # and every other endpoint keeps the ordinary small limit
    assert client.post("/listings", content=b"x" * (65 * 1024)).status_code == 413


def test_the_import_limit_is_big_enough_for_a_real_source_file() -> None:
    assert import_auth.IMPORT_MAX_BODY_BYTES >= 8 * 1024 * 1024  # the x402 bazaar file is ~8.5 MB today


# ---- dry run --------------------------------------------------------------------------------------


def test_dry_run_returns_counts_and_writes_nothing(audit_lines) -> None:
    source = _source()
    records = [_record() for _ in range(3)] + [{"name": "no endpoint"}]
    response = _post(records, source=source, dry_run="true")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["dry_run"], body["applied"]) == (True, False)
    assert (body["records"], body["valid"], body["added"], body["updated"], body["stale"], body["rejected"]) == (
        4, 3, 3, 0, 0, 1,
    )
    assert body["rejected_details"][0]["index"] == 3
    assert body["sources"] == [source]
    for record in records[:3]:
        assert _exists(source, record) == {}  # nothing was written
    entry = _audit_entries(audit_lines)[-1]
    assert entry["outcome"] == "dry_run" and entry["added"] == 3 and entry["rejected"] == 1


def test_dry_run_is_the_default_and_writing_needs_dry_run_false() -> None:
    source, record = _source(), _record()
    body = _post([record], source=source).json()
    assert body["dry_run"] is True and body["applied"] is False
    assert _exists(source, record) == {}
    assert _post([record], source=source, dry_run="false").json()["applied"] is True
    assert len(_exists(source, record)) == 1


def test_a_bad_parameter_value_is_a_422_after_the_key_is_accepted() -> None:
    assert_error(_post([_record()], dry_run="banana"), 422, "validation_error")
    assert_error(_post([_record()], mark_missing="maybe"), 422, "validation_error")


def test_dry_run_predicts_exactly_what_applying_does() -> None:
    source = _source()
    first = [_record(name=f"Pred {i} {uuid.uuid4().hex[:6]}") for i in range(4)]
    _post(first, source=source, dry_run="false")
    second = first[:3] + [_record()]  # three already there, one new, one gone
    preview = _post(second, source=source, dry_run="true").json()
    applied = _post(second, source=source, dry_run="false").json()
    for field in ("added", "updated", "stale", "rejected", "valid", "skipped_do_not_import"):
        assert preview[field] == applied[field], field
    assert (applied["added"], applied["updated"], applied["stale"]) == (1, 3, 1)


# ---- the sync semantics (the same as the script's) -------------------------------------------------


def test_apply_adds_then_updates_by_source_and_endpoint() -> None:
    source = _source()
    record = _record(name="Original Name")
    first = _post([record], source=source, dry_run="false").json()
    assert (first["added"], first["updated"]) == (1, 0)
    listing_id = _get(source, record)["id"]

    changed = {**record, "name": "Renamed By Source"}
    second = _post([changed], source=source, dry_run="false").json()
    assert (second["added"], second["updated"]) == (0, 1)
    after = _get(source, changed)
    assert after["id"] == listing_id and after["name"] == "Renamed By Source"
    assert after["claimed"] is False and after["source"] == source


def test_repeated_endpoints_in_one_batch_are_one_write_and_the_last_wins() -> None:
    source = _source()
    record = _record(name="First Copy")
    body = _post([record, {**record, "name": "Last Copy"}], source=source, dry_run="false").json()
    assert (body["added"], body["updated"], body["duplicates_in_batch"]) == (1, 0, 1)
    assert _get(source, {**record, "name": "Last Copy"})["name"] == "Last Copy"


def test_source_comes_from_each_record_when_not_forced() -> None:
    s1, s2 = _source(), _source()
    a = _record(_import={"source": s1})
    b = _record(_import={"source": s2})
    body = _post([a, b], dry_run="false").json()
    assert body["sources"] == sorted([s1, s2]) and body["added"] == 2
    assert _get(s1, a)["source"] == s1 and _get(s2, b)["source"] == s2


def test_a_claimed_listings_content_is_never_modified() -> None:
    source = _source()
    owner = Account.create()
    record = _record(name="Claimable Listing", payment_wallet=owner.address)
    _post([record], source=source, dry_run="false")
    listing_id = _get(source, record)["id"]

    claim = client.post(
        f"/listings/{listing_id}/claim",
        headers={"X-Wallet-Auth": wallet_auth_header(owner, action="claim-listing", listing_id=listing_id)},
    )
    assert claim.status_code == 200
    patch = {"name": "Owner Edited"}
    client.patch(
        f"/listings/{listing_id}", json=patch,
        headers={"X-Wallet-Auth": wallet_auth_header(owner, action="update-listing", listing_id=listing_id, body=patch)},
    )

    changed = {**record, "name": "Source Overwrite Attempt", "description": "different", "output_schema": None}
    preview = _post([changed], source=source, dry_run="true").json()
    applied = _post([changed], source=source, dry_run="false").json()
    assert preview["claimed_content_preserved"] == applied["claimed_content_preserved"] == 1
    after = client.get(f"/listings/{listing_id}").json()
    assert after["name"] == "Owner Edited"
    assert after["description"] == record["description"]
    assert after["claimed"] is True and after["output_schema"] == record["output_schema"]


def test_do_not_import_is_honored() -> None:
    source = _source()
    record = _record(name="Remove Me")
    _post([record], source=source, dry_run="false")
    listing_id = _get(source, record)["id"]
    assert db.remove_imported_listing(listing_id, "owner asked", datetime.now(timezone.utc)) is not None

    preview = _post([record], source=source, dry_run="true").json()
    applied = _post([record], source=source, dry_run="false").json()
    assert preview["skipped_do_not_import"] == applied["skipped_do_not_import"] == 1
    assert (applied["added"], applied["updated"]) == (0, 0)
    assert _exists(source, record) == {}


def test_listings_missing_from_the_batch_are_flagged_stale_not_deleted() -> None:
    source = _source()
    keep, drop = _record(name="Keeper"), _record(name="Dropped")
    _post([keep, drop], source=source, dry_run="false")
    drop_id = _get(source, drop)["id"]
    assert client.get(f"/listings/{drop_id}").json()["stale"] is False

    preview = _post([keep], source=source, dry_run="true").json()
    assert preview["stale"] == 1
    assert client.get(f"/listings/{drop_id}").json()["stale"] is False  # a dry run flags nothing

    applied = _post([keep], source=source, dry_run="false").json()
    assert applied["stale"] == 1
    flagged = client.get(f"/listings/{drop_id}")
    assert flagged.status_code == 200 and flagged.json()["stale"] is True  # still there, still readable
    assert client.get(f"/listings/{drop_id}/template").status_code == 200

    # flagged once: running it again does not count it again, and it comes back when it returns
    assert _post([keep], source=source, dry_run="false").json()["stale"] == 0
    _post([keep, drop], source=source, dry_run="false")
    assert client.get(f"/listings/{drop_id}").json()["stale"] is False


def test_mark_missing_false_leaves_the_rest_of_the_source_alone() -> None:
    source = _source()
    a, b = _record(), _record()
    _post([a, b], source=source, dry_run="false")
    body = _post([a], source=source, dry_run="false", mark_missing="false").json()
    assert body["stale"] == 0 and body["mark_missing"] is False
    assert client.get(f"/listings/{_get(source, b)['id']}").json()["stale"] is False


def test_a_batch_with_no_valid_records_never_stales_a_source() -> None:
    source = _source()
    record = _record()
    _post([record], source=source, dry_run="false")
    listing_id = _get(source, record)["id"]

    all_rejected = _post([{"name": "broken"}, {"name": "also broken"}], source=source, dry_run="false")
    assert all_rejected.status_code == 200
    body = all_rejected.json()
    assert (body["valid"], body["rejected"], body["stale"], body["added"], body["updated"]) == (0, 2, 0, 0, 0)
    assert client.get(f"/listings/{listing_id}").json()["stale"] is False

    assert_error(_post("", source=source, dry_run="false"), 422, "validation_error")
    assert_error(_post("\n\n", source=source, dry_run="false"), 422, "validation_error")
    assert client.get(f"/listings/{listing_id}").json()["stale"] is False


def test_invalid_records_are_rejected_individually_and_reported() -> None:
    source = _source()
    good = _record()
    body = _post([good, {"name": "x"}, "not an object", _record(payment_wallet="nope", payment_options=[])],
                 source=source, dry_run="false").json()
    assert (body["valid"], body["added"], body["rejected"]) == (1, 1, 3)
    assert [d["index"] for d in body["rejected_details"]] == [1, 2, 3]
    assert all(d["reason"] for d in body["rejected_details"])


def test_a_batch_that_is_not_json_is_a_422_naming_the_line() -> None:
    body = assert_error(_post('{"ok": 1}\n{broken\n', dry_run="true"), 422, "validation_error")
    assert "line 2" in body["message"]
    assert_error(_post(b"\xff\xfe\x00", dry_run="true"), 422, "validation_error")


def test_a_json_array_is_accepted_like_the_script_accepts_one() -> None:
    source, record = _source(), _record()
    body = _post(json.dumps([record]), source=source, dry_run="false").json()
    assert body["added"] == 1


def test_only_one_import_runs_at_a_time() -> None:
    assert admin_import._import_running.acquire(blocking=False)
    try:
        assert_error(_post([_record()], source=_source()), 409, "conflict")
    finally:
        admin_import._import_running.release()
    assert _post([_record()], source=_source()).status_code == 200  # and the lock was not left held


def test_the_endpoint_and_the_script_do_the_same_thing(tmp_path, capsys) -> None:
    records = [_record(name=f"Parity {i} {uuid.uuid4().hex[:6]}") for i in range(3)]
    records.append({"name": "invalid"})
    api_source, script_source = _source(), _source()

    api = _post(records, source=api_source, dry_run="false").json()
    path = tmp_path / "records.jsonl"
    path.write_text(_jsonl(records), encoding="utf-8")
    assert bulk_import.main(["--file", str(path), "--source", script_source, "--apply", "--yes",
                             "--allow-local-board-url", "--log-file", str(tmp_path / "log")]) == 0
    out = capsys.readouterr().out
    assert f"Inserted {api['added']}, updated {api['updated']}, newly marked missing {api['stale']}" in out
    assert f"{api['valid']} record(s) valid, {api['rejected']} rejected" in out

    for record in records[:3]:
        via_api, via_script = _get(api_source, record), _get(script_source, record)
        assert via_api["template_url"] == f"{BASE_URL}/listings/{via_api['id']}/template"
        assert via_script["template_url"] == f"{BASE_URL}/listings/{via_script['id']}/template"
        for field in ("name", "description", "task_categories", "payment_wallet", "pricing_amount", "output_schema"):
            assert via_api[field] == via_script[field], field


def _script_apply(tmp_path, *extra) -> tuple[int, str, str, dict]:
    record = _record()
    path = tmp_path / "r.json"
    path.write_text(json.dumps([record]), encoding="utf-8")
    source = _source()
    code = bulk_import.main(["--file", str(path), "--source", source, "--apply", "--yes",
                             "--log-file", str(tmp_path / "log"), *extra])
    return code, source, record


def test_the_script_refuses_to_apply_without_a_board_url(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("SERVICE_BASE_URL")
    code, source, record = _script_apply(tmp_path)
    assert code == 1 and "no board URL" in capsys.readouterr().err
    assert _exists(source, record) == {}


def test_the_script_refuses_a_local_board_url_unless_told_it_is_deliberate(tmp_path, capsys) -> None:
    # the usual mistake: an operator shell has SERVICE_BASE_URL=http://127.0.0.1:... in .env while
    # DATABASE_URL points at production - that must not write localhost links into production
    assert os.environ["SERVICE_BASE_URL"].startswith("http://127.0.0.1")
    code, source, record = _script_apply(tmp_path)
    assert code == 1 and "local address" in capsys.readouterr().err
    assert _exists(source, record) == {}


def test_the_script_builds_links_from_an_explicit_board_url(tmp_path, capsys) -> None:
    code, source, record = _script_apply(tmp_path, "--board-url", "https://board.example.test/")
    assert code == 0
    assert "https://board.example.test/listings/{id}/template" in capsys.readouterr().out
    listing = _get(source, record)
    assert listing["template_url"] == f"https://board.example.test/listings/{listing['id']}/template"


def test_the_script_refuses_a_non_http_board_url(tmp_path, capsys) -> None:
    code, source, record = _script_apply(tmp_path, "--board-url", "ftp://board.example.test")
    assert code == 1 and "not an http(s) URL" in capsys.readouterr().err


def test_local_urls_are_recognized() -> None:
    from app.core.import_sync import is_local_url

    for local in ("http://127.0.0.1:8200", "http://localhost:3000", "http://[::1]:8000", "http://x.localhost", "http://0.0.0.0"):
        assert is_local_url(local), local
    for public in ("https://agent-discovery-board.onrender.com", "https://board.example.test", "https://127.example.com"):
        assert not is_local_url(public), public


# ---- template links point at the board itself ------------------------------------------------------


def test_every_imported_listings_template_url_is_the_boards_own_route() -> None:
    source = _source()
    with_schema, without_schema = _record(name="Has Schema"), _record(name="No Schema", output_schema=None)
    _post([with_schema, without_schema], source=source, dry_run="false")

    listing = _get(source, with_schema)
    assert listing["template_url"] == f"{BASE_URL}/listings/{listing['id']}/template"  # not the GitHub link
    template = client.get(listing["template_url"].replace(BASE_URL, "")).json()
    assert template["output_schema"] == with_schema["output_schema"]
    assert template["template_url"] == listing["template_url"]  # the link resolves to itself

    bare = _get(source, without_schema)
    assert bare["template_url"] is None  # that route would 404: no dangling link
    assert client.get(f"/listings/{bare['id']}/template").status_code == 404


def test_a_resync_keeps_the_link_on_the_listings_existing_id() -> None:
    source, record = _source(), _record()
    _post([record], source=source, dry_run="false")
    first = _get(source, record)
    _post([{**record, "name": record["name"] + " v2"}], source=source, dry_run="false")
    again = _get(source, {**record, "name": record["name"] + " v2"})
    assert again["id"] == first["id"]
    assert again["template_url"] == f"{BASE_URL}/listings/{first['id']}/template"


def test_a_schema_added_or_removed_by_the_source_updates_the_link() -> None:
    source = _source()
    record = _record(output_schema=None)
    _post([record], source=source, dry_run="false")
    assert _get(source, record)["template_url"] is None
    _post([{**record, "output_schema": {"type": "object"}}], source=source, dry_run="false")
    listing = _get(source, record)
    assert listing["template_url"] == f"{BASE_URL}/listings/{listing['id']}/template"
    _post([record], source=source, dry_run="false")
    assert _get(source, record)["template_url"] is None


def test_import_upsert_without_a_template_base_keeps_the_rows_own_link() -> None:
    # the db layer's default: only callers that pass template_base get the board's own link
    from app.core.imports import build_import_row

    row = build_import_row(_record(), source=_source(), now=datetime.now(timezone.utc))
    saved, inserted = db.import_upsert(row)
    assert inserted and saved["template_url"] == row["template_url"]


# ---- audit: one line per call, counts only, never the key ------------------------------------------


def test_every_call_writes_exactly_one_audit_line_of_counts_only(audit_lines) -> None:
    source = _source()
    record = _record(name="Secret Listing Name")
    _post([record], source=source, dry_run="true")
    _post([record], source=source, dry_run="false")
    _post([record], headers={"Authorization": "Bearer wrong-key-value"}, source=source)
    _post([record], headers={}, dry_run="false")
    _post("{bad", dry_run="true")

    entries = _audit_entries(audit_lines)
    assert [e["outcome"] for e in entries] == ["dry_run", "applied", "unauthorized", "unauthorized", "bad_batch"]
    assert [e["status"] for e in entries] == [200, 200, 401, 401, 422]
    applied = entries[1]
    assert applied["added"] == 1 and applied["valid"] == 1 and applied["records"] == 1 and applied["bytes"] > 0
    assert applied["dry_run"] is False and applied["source"] == source and "duration_ms" in applied
    assert all("client" in e and "ts" in e and e["event"] == "admin_import" for e in entries)

    everything = "\n".join(audit_lines)
    for secret in (KEY, "wrong-key-value", "Secret Listing Name", record["endpoint_url"], record["payment_wallet"]):
        assert secret not in everything
    # an unauthenticated caller's parameters were never read, so they cannot appear either
    assert "source" not in entries[2] and "dry_run" not in entries[2]


def test_an_error_during_the_sync_is_audited_and_not_swallowed(monkeypatch, audit_lines) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("database fell over")

    monkeypatch.setattr(admin_import.import_sync, "plan_sync", boom)
    with pytest.raises(RuntimeError):
        _post([_record()], source=_source())
    entry = _audit_entries(audit_lines)[-1]
    assert (entry["outcome"], entry["status"]) == ("error", 500)
    assert "database fell over" not in "\n".join(audit_lines)
    assert not admin_import._import_running.locked()


def test_the_response_never_contains_the_key() -> None:
    response = _post([_record()], source=_source(), dry_run="false")
    assert KEY not in response.text and os.environ["IMPORT_API_KEY"] not in response.text
