"""Optional task due dates (``tasks.due_at``).

Dates used to live only in card titles and bodies ("by Nov 30"), so nothing could
watch them. ``due_at`` is optional: an undated card stays exactly as before.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_due as kdue
from tools import kanban_tools as kt

# 2026-11-30T17:00:00-06:00
DUE_ISO = "2026-11-30T17:00:00-06:00"
DUE_EPOCH = 1796079600


@pytest.fixture
def kanban_home(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _cli(verb, *argv):
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", verb, *argv])
    return {"create": kc._cmd_create, "edit": kc._cmd_edit, "show": kc._cmd_show, "list": kc._cmd_list}[verb](args)


def _stored(task_id):
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, task_id)


def test_parse_due_requires_an_offset():
    assert kdue.parse_due(DUE_ISO) == DUE_EPOCH
    assert kdue.parse_due("2026-11-30T23:00:00Z") == DUE_EPOCH
    assert kdue.parse_due(None) is None
    # A naive stamp would be read in the host zone and silently move the deadline.
    with pytest.raises(ValueError, match="UTC offset"):
        kdue.parse_due("2026-11-30T17:00:00")
    with pytest.raises(ValueError, match="ISO 8601"):
        kdue.parse_due("by Nov 30")


@pytest.mark.parametrize("delta, label", [
    (13 * 86400 + 5, "due in 13d"), (5 * 3600 + 10, "due in 5h"), (20 * 60, "due in 20m"),
    (10, "due in <1m"), (-2 * 3600, "overdue 2h"), (-3 * 86400, "overdue 3d"),
])
def test_format_due_is_relative(delta, label):
    assert kdue.format_due(1_000_000 + delta, now=1_000_000) == label


def test_format_due_unset_is_empty():
    assert kdue.format_due(None) == ""


def test_cli_create_edit_show_list_round_trip(kanban_home, capsys):
    assert _cli("create", "Warpspeed", "--due", DUE_ISO, "--json") == 0
    task_id = json.loads(capsys.readouterr().out)["id"]
    assert _stored(task_id).due_at == DUE_EPOCH

    assert _cli("show", task_id) == 0
    assert "  due:" in capsys.readouterr().out
    assert _cli("list") == 0
    listed = capsys.readouterr().out
    assert "(due in" in listed or "(overdue" in listed

    assert _cli("edit", task_id, "--due", "2026-12-01T00:00:00Z") == 0
    assert _stored(task_id).due_at == 1796083200
    with kbc.connect_closing() as conn:
        edited = [e for e in kb.list_events(conn, task_id) if e.kind == "edited"]
    assert edited[-1].payload == {"fields": ["due_at"], "due_at": 1796083200}

    assert _cli("edit", task_id, "--due", "none") == 0
    assert _stored(task_id).due_at is None


def test_undated_card_shows_no_due_marker(kanban_home, capsys):
    assert _cli("create", "Plain card", "--json") == 0
    task_id = json.loads(capsys.readouterr().out)["id"]
    assert _stored(task_id).due_at is None
    assert _cli("show", task_id) == 0
    assert "  due:" not in capsys.readouterr().out
    assert _cli("list") == 0
    out = capsys.readouterr().out
    assert "due in" not in out and "overdue" not in out


def test_cli_rejects_naive_due_without_writing(kanban_home, capsys):
    assert _cli("create", "card", "--due", "2026-11-30") == 2
    assert "--due" in capsys.readouterr().err
    with kbc.connect_closing() as conn:
        assert kb.list_tasks(conn) == []


def test_edit_due_on_unknown_task_fails(kanban_home, capsys):
    assert _cli("edit", "t_nope", "--due", DUE_ISO) == 1


def test_kanban_create_tool_accepts_due(kanban_home):
    out = json.loads(kt._handle_create({"title": "card", "assignee": "peer", "due": DUE_ISO}))
    assert out["ok"] is True
    assert _stored(out["task_id"]).due_at == DUE_EPOCH
    bad = json.loads(kt._handle_create({"title": "card", "assignee": "peer", "due": "Nov 30"}))
    assert bad.get("ok") is not True and "ISO 8601" in bad["error"]
    plain = json.loads(kt._handle_create({"title": "card", "assignee": "peer"}))
    assert _stored(plain["task_id"]).due_at is None


def test_existing_board_gains_nullable_due_at(kanban_home):
    """A board created before the column existed is migrated in place on open:
    existing rows keep working and read as undated."""
    db_path = kb.kanban_db_path()
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="old card", assignee="peer")
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("ALTER TABLE tasks DROP COLUMN due_at")
        raw.commit()
        assert "due_at" not in {r[1] for r in raw.execute("PRAGMA table_info(tasks)")}
    finally:
        raw.close()
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        assert "due_at" in {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert kb.get_task(conn, task_id).due_at is None
        assert kdue.set_due_at(conn, task_id, DUE_EPOCH) is True
        assert kb.get_task(conn, task_id).due_at == DUE_EPOCH
