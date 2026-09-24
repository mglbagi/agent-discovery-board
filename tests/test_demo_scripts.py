"""The demo scripts' safety rails: local-only by default, everything they create is a
temporary test- listing on an unresolvable endpoint, and everything is cleaned up - even
when a demo fails halfway."""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app.core import db
from app.core.models import ListingCreate
from app.main import app
from tests.helpers import db_row, listing_payload

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import demo_safety  # noqa: E402


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worked_example = _load("worked_example")
agent_view = _load("agent_view")

PRODUCTION = "https://agent-discovery-board.onrender.com"


def _wipe_test_rows() -> None:
    db.purge_expired_test_listings(datetime.now(timezone.utc) + timedelta(days=3650), 100_000)


@pytest.fixture(autouse=True)
def _clean():
    _wipe_test_rows()
    yield
    _wipe_test_rows()


class Sentinel(Exception):
    pass


# ---- refusing non-local targets ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8200", "http://localhost:8200", "https://localhost", "http://[::1]:8200", "http://testserver"],
)
def test_local_urls_are_allowed(url: str) -> None:
    assert demo_safety.is_local_url(url)
    demo_safety.require_safe_target(url, allow_production=False)


@pytest.mark.parametrize(
    "url",
    [PRODUCTION, "https://example.com", "https://127.0.0.1.evil.com", "https://evil.com/127.0.0.1", "http://192.168.1.10:8200", "https://localhost.evil.com"],
)
def test_non_local_urls_are_refused_without_the_flag(url: str, capsys) -> None:
    assert not demo_safety.is_local_url(url)
    with pytest.raises(SystemExit) as exc:
        demo_safety.require_safe_target(url, allow_production=False)
    assert exc.value.code == demo_safety.REFUSED_EXIT_CODE
    assert "--allow-production" in capsys.readouterr().err
    demo_safety.require_safe_target(url, allow_production=True)  # explicit opt-in passes


@pytest.mark.parametrize("script", [worked_example, agent_view])
def test_a_script_refuses_production_before_making_any_request(script, monkeypatch) -> None:
    def no_client(base_url):
        raise AssertionError("a client was created: the guard did not run first")

    monkeypatch.setattr(demo_safety, "make_client", no_client)
    with pytest.raises(SystemExit) as exc:
        script.main(["--url", PRODUCTION])
    assert exc.value.code == 2


@pytest.mark.parametrize("script", [worked_example, agent_view])
def test_allow_production_lets_the_script_proceed(script, monkeypatch) -> None:
    def marker_client(base_url):
        raise Sentinel(base_url)

    monkeypatch.setattr(demo_safety, "make_client", marker_client)
    with pytest.raises(Sentinel) as exc:
        script.main(["--url", PRODUCTION, "--allow-production"])
    assert exc.value.args[0] == PRODUCTION


# ---- names and endpoints ----------------------------------------------------------------------------------


def test_demo_names_and_endpoints_are_test_listings_that_can_never_resolve() -> None:
    marker = demo_safety.new_marker()
    name, url = demo_safety.demo_name("thing", marker), demo_safety.demo_endpoint(marker, "/a/b")
    assert name.startswith("test-") and marker in name
    host = urlparse(url).hostname
    assert host == f"test-{marker}.example.invalid" and url.startswith("https://")
    owner = Account.create()
    ListingCreate(**listing_payload("offering", owner.address, name=name, endpoint_url=url))  # passes real validation


# ---- DemoSession: cleanup no matter what -------------------------------------------------------------------


def _payload(owner, marker: str, suffix: str) -> dict:
    return listing_payload(
        "offering", owner.address, name=demo_safety.demo_name(suffix, marker), endpoint_url=demo_safety.demo_endpoint(marker, f"/{suffix}")
    )


def test_a_session_deactivates_everything_it_created() -> None:
    http, owner, marker = TestClient(app), Account.create(), demo_safety.new_marker()
    with demo_safety.DemoSession(http, log=lambda *_: None) as demo:
        ids = [demo.create(_payload(owner, marker, s), owner).json()["id"] for s in ("a", "b", "c")]
        assert all(db_row(i)["status"] == "active" for i in ids)
    assert all(db_row(i)["status"] == "inactive" for i in ids)


def test_cleanup_runs_even_when_the_demo_raises() -> None:
    http, owner, marker = TestClient(app), Account.create(), demo_safety.new_marker()
    ids: list[str] = []
    with pytest.raises(RuntimeError, match="demo blew up"):
        with demo_safety.DemoSession(http, log=lambda *_: None) as demo:
            ids.append(demo.create(_payload(owner, marker, "a"), owner).json()["id"])
            ids.append(demo.create(_payload(owner, marker, "b"), owner).json()["id"])
            raise RuntimeError("demo blew up")
    assert [db_row(i)["status"] for i in ids] == ["inactive", "inactive"]  # the original error still propagates


def test_a_failing_cleanup_of_one_listing_does_not_stop_the_rest_or_mask_the_error() -> None:
    http, owner, marker = TestClient(app), Account.create(), demo_safety.new_marker()
    messages: list[str] = []
    real_delete = http.delete
    calls = {"n": 0}

    def flaky_delete(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("network hiccup")
        return real_delete(url, **kw)

    http.delete = flaky_delete
    ids: list[str] = []
    with pytest.raises(RuntimeError, match="original"):
        with demo_safety.DemoSession(http, log=messages.append) as demo:
            ids += [demo.create(_payload(owner, marker, s), owner).json()["id"] for s in ("a", "b")]
            raise RuntimeError("original")
    statuses = sorted(db_row(i)["status"] for i in ids)
    assert statuses == ["active", "inactive"]  # one failed, the other was still cleaned
    assert any("FAILED" in m for m in messages) and any("purges them itself" in m for m in messages)


def test_a_session_refuses_a_non_test_name_and_creates_nothing() -> None:
    http, owner = TestClient(app), Account.create()
    with demo_safety.DemoSession(http, log=lambda *_: None) as demo:
        with pytest.raises(ValueError, match="test-"):
            demo.create(listing_payload("offering", owner.address, name="A real-looking name"), owner)
        assert demo.created == []


def test_failed_creates_are_not_tracked() -> None:
    http, owner, marker = TestClient(app), Account.create(), demo_safety.new_marker()
    with demo_safety.DemoSession(http, log=lambda *_: None) as demo:
        first = demo.create(_payload(owner, marker, "a"), owner)
        dup = demo.create(_payload(owner, marker, "a"), owner)  # 409
        assert first.status_code == 201 and dup.status_code == 409
        assert len(demo.created) == 1


# ---- the scripts end to end ---------------------------------------------------------------------------------


def _record_created(monkeypatch) -> list[str]:
    created: list[str] = []
    real_create = demo_safety.DemoSession.create

    def recording_create(self, payload, account):
        response = real_create(self, payload, account)
        if response.status_code == 201:
            created.append(response.json()["id"])
        return response

    monkeypatch.setattr(demo_safety.DemoSession, "create", recording_create)
    return created


def _assert_clean(created: list[str]) -> None:
    assert created, "the run created nothing"
    for listing_id in created:
        row = db_row(listing_id)
        assert row["is_test"] is True and row["name"].startswith("test-")
        assert urlparse(row["endpoint_url"]).hostname.endswith(".example.invalid")
        assert row["status"] == "inactive"  # cleaned up
    visible = TestClient(app).get("/listings", params={"limit": 100}).json()["listings"]
    assert not set(created) & {i["id"] for i in visible}  # and never visible in normal browsing


def test_worked_example_runs_end_to_end_cleanly(monkeypatch, capsys) -> None:
    created = _record_created(monkeypatch)
    monkeypatch.setattr(demo_safety, "make_client", lambda base_url: TestClient(app))
    assert worked_example.main(["--url", "http://127.0.0.1:8200"]) == 0
    out = capsys.readouterr().out
    assert "All worked-example steps completed successfully." in out and "cleanup: deactivated" in out
    assert len(created) == 5  # 4 types + a second announcement
    _assert_clean(created)


def test_worked_example_cleans_up_when_it_fails_halfway(monkeypatch) -> None:
    created = _record_created(monkeypatch)

    class FailingClient(TestClient):
        def patch(self, *a, **kw):
            raise RuntimeError("simulated failure mid-demo")

    monkeypatch.setattr(demo_safety, "make_client", lambda base_url: FailingClient(app))
    with pytest.raises(RuntimeError, match="simulated failure"):
        worked_example.main(["--url", "http://127.0.0.1:8200"])
    assert len(created) == 4  # the four listings made before the failure
    _assert_clean(created)


def test_the_raw_view_script_source_uses_the_rails() -> None:
    # (Its full run needs a real HTTP server for the MCP calls: see tests/test_mcp_search.py.)
    source = (SCRIPTS / "agent_view.py").read_text(encoding="utf-8")
    for needle in ("require_safe_target", "--allow-production", "DemoSession", "demo_name(", "demo_endpoint("):
        assert needle in source
