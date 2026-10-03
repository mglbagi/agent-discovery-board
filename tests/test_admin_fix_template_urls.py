"""scripts/admin_fix_template_urls.py: the one-time fix that gives stale imported listings the
board's own template_url. Touches only template_url, only on unclaimed, source-stale imported
listings that have an output_schema and still point off-board."""

import importlib.util
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from eth_account import Account
from psycopg.rows import dict_row

from app.core import db
from app.core.imports import build_import_row

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("admin_fix_template_urls", ROOT / "scripts" / "admin_fix_template_urls.py")
fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fix)

BASE = os.environ["SERVICE_BASE_URL"]
GITHUB = "https://raw.githubusercontent.com/example/repo/main/templates/vt_x.json"


def _import(source: str, **overrides) -> dict:
    record = {
        "name": f"Fix{uuid.uuid4().hex[:10]}",
        "description": "Imported for the template-url fix test.",
        "task_categories": ["other"],
        "endpoint_url": f"https://example.com/fix/{uuid.uuid4().hex}",
        "payment_wallet": Account.create().address,
        "pricing_model": "per_call",
        "pricing_amount": "$0.01",
        "output_schema": {"type": "object"},
        "template_url": GITHUB,
    }
    record.update(overrides)
    row = build_import_row(record, source=source, now=datetime.now(timezone.utc))
    saved, _ = db.import_upsert(row)  # no template_base: keeps the record's own (off-board) link
    return saved


def _fetch(listing_id: str) -> dict:
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        return conn.execute("SELECT * FROM listings WHERE id = %s", (listing_id,)).fetchone()


def _run(capsys, tmp_path, *argv):
    log = tmp_path / "admin.log"
    code = fix.main(["--allow-local-board-url"] + list(argv) + ["--log-file", str(log)])
    out = capsys.readouterr()
    return code, out.out, out.err, log


def _count(out: str) -> int:
    return int(re.search(r"^(\d+) stale, unclaimed imported", out, re.M).group(1))


@pytest.fixture
def scenario():
    """One source with every kind of row the fix must either change or leave alone."""
    source = "fix-" + uuid.uuid4().hex[:8]
    rows = {
        "target_a": _import(source),
        "target_b": _import(source),
        "target_null": _import(source, template_url=None),
        "not_stale": _import(source),
        "already_board": _import(source),
        "no_schema": _import(source, output_schema=None),
        "claimed": _import(source),
    }
    # Everything except not_stale is absent from the "sync" -> marked missing (stale).
    db.mark_missing_from_source(source, [rows["not_stale"]["endpoint_url"]], datetime.now(timezone.utc))
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        board_link = f"{BASE}/listings/{rows['already_board']['id']}/template"
        conn.execute("UPDATE listings SET template_url = %s WHERE id = %s", (board_link, rows["already_board"]["id"]))
        conn.execute("UPDATE listings SET claimed = TRUE WHERE id = %s", (rows["claimed"]["id"],))
        conn.commit()
    return rows


def test_dry_run_counts_and_writes_nothing(scenario, capsys, tmp_path) -> None:
    before = {k: _fetch(v["id"]) for k, v in scenario.items()}
    code, out, err, log = _run(capsys, tmp_path)
    assert code == 0, err
    assert "DRY RUN" in out and _count(out) >= 3
    assert {k: _fetch(v["id"]) for k, v in scenario.items()} == before
    assert not log.exists()


def test_apply_changes_only_template_url_on_exactly_the_matching_rows(scenario, capsys, tmp_path) -> None:
    before = {k: _fetch(v["id"]) for k, v in scenario.items()}
    _, dry_out, _, _ = _run(capsys, tmp_path)
    n = _count(dry_out)

    code, out, err, log = _run(capsys, tmp_path, "--apply", "--expect-count", str(n), "--yes")
    assert code == 0, err
    assert f"Updated {n} listing(s)" in out

    after = {k: _fetch(v["id"]) for k, v in scenario.items()}
    for key in ("target_a", "target_b", "target_null"):
        assert after[key]["template_url"] == f"{BASE}/listings/{scenario[key]['id']}/template", key
        # every other column - including updated_at and the sync timestamps - is untouched
        assert {c: v for c, v in after[key].items() if c != "template_url"} == {
            c: v for c, v in before[key].items() if c != "template_url"
        }, key
    for key in ("not_stale", "already_board", "no_schema", "claimed"):
        assert after[key] == before[key], key  # byte-for-byte the same row

    # a second run finds nothing left to do
    _, again, _, _ = _run(capsys, tmp_path)
    assert _count(again) == 0


def test_apply_is_logged_with_before_and_after(scenario, capsys, tmp_path) -> None:
    _, dry_out, _, _ = _run(capsys, tmp_path)
    n = _count(dry_out)
    code, _, err, log = _run(capsys, tmp_path, "--apply", "--expect-count", str(n), "--yes")
    assert code == 0, err
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["action"] == "fix-template-urls" and entry["changed_column"] == "template_url" and entry["count"] == n
    assert entry["operator"] and entry["db"] and entry["ts"]
    by_id = {item["id"]: item for item in entry["listings"]}
    target = by_id[scenario["target_a"]["id"]]
    assert target["template_url_before"] == GITHUB
    assert target["template_url_after"] == f"{BASE}/listings/{scenario['target_a']['id']}/template"
    assert by_id[scenario["target_null"]["id"]]["template_url_before"] is None
    assert scenario["claimed"]["id"] not in by_id and scenario["not_stale"]["id"] not in by_id
    assert "postgres" not in log.read_text(encoding="utf-8")  # host only, never credentials


def test_apply_without_an_expected_count_is_refused(scenario, capsys, tmp_path) -> None:
    before = _fetch(scenario["target_a"]["id"])
    code, _, err, log = _run(capsys, tmp_path, "--apply", "--yes")
    assert code == 2 and "--expect-count" in err
    assert _fetch(scenario["target_a"]["id"]) == before and not log.exists()


def test_apply_with_the_wrong_count_writes_nothing(scenario, capsys, tmp_path) -> None:
    before = _fetch(scenario["target_a"]["id"])
    _, dry_out, _, _ = _run(capsys, tmp_path)
    code, _, err, log = _run(capsys, tmp_path, "--apply", "--expect-count", str(_count(dry_out) + 1), "--yes")
    assert code == 2 and "nothing written" in err
    assert _fetch(scenario["target_a"]["id"]) == before and not log.exists()


def test_confirmation_must_match(scenario, capsys, tmp_path, monkeypatch) -> None:
    before = _fetch(scenario["target_a"]["id"])
    _, dry_out, _, _ = _run(capsys, tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": "wrong")
    code, _, err, _ = _run(capsys, tmp_path, "--apply", "--expect-count", str(_count(dry_out)))
    assert code == 2 and "nothing written" in err
    assert _fetch(scenario["target_a"]["id"]) == before


def test_refuses_without_a_board_url(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.delenv("SERVICE_BASE_URL")
    code, _, err, _ = _run(capsys, tmp_path)
    assert code == 1 and "no board URL" in err


def test_refuses_a_local_board_url_unless_told_it_is_deliberate(scenario, capsys, tmp_path) -> None:
    # an operator shell has SERVICE_BASE_URL=http://127.0.0.1:... while DATABASE_URL is production
    before = _fetch(scenario["target_a"]["id"])
    log = tmp_path / "admin.log"
    code = fix.main(["--apply", "--expect-count", "1", "--yes", "--log-file", str(log)])
    err = capsys.readouterr().err
    assert code == 1 and "local address" in err
    assert _fetch(scenario["target_a"]["id"]) == before and not log.exists()


def test_builds_links_from_an_explicit_board_url(scenario, capsys, tmp_path) -> None:
    _, dry_out, _, _ = _run(capsys, tmp_path, "--board-url", "https://board.example.test")
    assert "https://board.example.test/listings/" in dry_out
    n = _count(dry_out)
    code, _, err, _ = _run(capsys, tmp_path, "--board-url", "https://board.example.test/", "--apply",
                           "--expect-count", str(n), "--yes")
    assert code == 0, err
    row = _fetch(scenario["target_a"]["id"])
    assert row["template_url"] == f"https://board.example.test/listings/{row['id']}/template"
