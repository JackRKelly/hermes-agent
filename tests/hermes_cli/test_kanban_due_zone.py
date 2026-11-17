"""Due dates are stored as UTC epoch seconds and shown in the owner's zone (Chicago by default),
labelled CST or CDT, across the 2027-03-14 daylight-saving change."""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import hermes_time
from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_due as kdue

# Saturday before and Monday after the US spring-forward on Sunday 2027-03-14.
CST_ISO, CST_UTC = "2027-03-13T17:00:00-06:00", datetime(2027, 3, 13, 23, 0, tzinfo=UTC)
CDT_ISO, CDT_UTC = "2027-03-15T17:00:00-05:00", datetime(2027, 3, 15, 22, 0, tzinfo=UTC)


@pytest.fixture
def owner_home(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hermes_time.reset_cache()
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    yield home
    hermes_time.reset_cache()


def _show(task_id, capsys):
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    assert kc._cmd_show(root.parse_args(["kanban", "show", task_id])) == 0
    return next(line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("due:"))


@pytest.mark.parametrize("iso, utc, shown", [
    (CST_ISO, CST_UTC, "2027-03-13 17:00 CST"),
    (CDT_ISO, CDT_UTC, "2027-03-15 17:00 CDT"),
])
def test_due_stores_utc_and_shows_chicago_across_dst(owner_home, capsys, iso, utc, shown):
    assert kdue.parse_due(iso) == int(utc.timestamp())
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    assert kc._cmd_create(root.parse_args(["kanban", "create", "card", "--due", iso, "--json"])) == 0
    task_id = json.loads(capsys.readouterr().out)["id"]
    # run_tests.sh pins TZ=UTC: the host zone would print 23:00 / 22:00 with no label.
    assert shown in _show(task_id, capsys)


def test_configured_profile_zone_wins_over_chicago(owner_home, monkeypatch):
    monkeypatch.setenv("HERMES_TIMEZONE", "Europe/London")
    hermes_time.reset_cache()
    assert kdue.format_due_at(int(CST_UTC.timestamp())) == "2027-03-13 23:00 GMT"


def test_edit_due_with_result_is_refused_before_any_write(owner_home, capsys):
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    assert kc._cmd_create(root.parse_args(["kanban", "create", "card", "--json"])) == 0
    task_id = json.loads(capsys.readouterr().out)["id"]
    args = root.parse_args(["kanban", "edit", task_id, "--due", CST_ISO, "--result", "x"])
    assert kc._cmd_edit(args) == 2
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, task_id).due_at is None
