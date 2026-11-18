"""``hermes kanban caps`` shows and edits the live concurrency caps.

Driven through the real argparse tree and ``kanban_command`` against a temp ``HERMES_HOME``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_caps
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.config import get_config_path


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    get_config_path().write_text("# my notes\nkanban:\n  max_in_progress: 10\n", encoding="utf-8")
    kb.init_db()
    return hermes_home


def _run(*argv: str) -> int:
    top = argparse.ArgumentParser(prog="hermes")
    kanban_cli.build_parser(top.add_subparsers(dest="command"))
    return kanban_cli.kanban_command(top.parse_args(["kanban", *argv]))


def test_set_writes_config_and_keeps_comments(home, capsys):
    assert _run("caps", "set", "--total", "14", "--per-profile", "7", "--profile", "builder=7") == 0
    text = get_config_path().read_text(encoding="utf-8")
    assert "# my notes" in text
    kanban = yaml.safe_load(text)["kanban"]
    assert kanban["max_in_progress"] == 14
    assert kanban["max_in_progress_per_profile"] == 7
    assert kanban["max_in_progress_by_profile"] == {"builder": 7}
    assert "no restart needed" in capsys.readouterr().out

    assert _run("caps", "set", "--clear-profile", "builder") == 0
    assert "max_in_progress_by_profile" not in yaml.safe_load(get_config_path().read_text())["kanban"]


def test_set_rejects_invalid_caps(home, capsys):
    assert _run("caps", "set", "--per-profile", "11") != 0
    assert "above max_in_progress=10" in capsys.readouterr().err
    assert yaml.safe_load(get_config_path().read_text())["kanban"] == {"max_in_progress": 10}


def test_set_refuses_a_config_the_dispatcher_does_not_read(home, tmp_path, capsys):
    kanban_caps.write_live_caps(kanban_caps.KanbanCaps(total=10), effective_total=10,
                                config_path=tmp_path / "elsewhere" / "config.yaml")
    assert _run("caps", "set", "--total", "12") != 0
    assert "the live dispatcher reads" in capsys.readouterr().err


def test_show_json_reports_lanes_and_live_state(home, capsys):
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="a", assignee="alpha")
    kanban_caps.write_live_caps(kanban_caps.KanbanCaps(total=10), effective_total=10, config_path=get_config_path())
    assert _run("caps", "--json") == 0
    snap = json.loads(capsys.readouterr().out)
    assert snap["configured"]["total"] == 10
    assert snap["in_sync"] is True
    assert snap["lanes"]["alpha"] == {"running": 0, "ready": 1, "cap": None}


def test_slash_can_read_but_not_set(home):
    assert "configured" in kanban_cli.run_slash("caps")
    assert "terminal-only" in kanban_cli.run_slash("caps set --total 12")
    assert yaml.safe_load(get_config_path().read_text())["kanban"] == {"max_in_progress": 10}
