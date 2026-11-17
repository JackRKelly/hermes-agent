"""``kanban_create`` binds a worktree card to a named branch, like ``hermes kanban create --branch``.

Dispatcher-spawned workers are fenced off the CLI, so the tool is their only creation
path; without ``branch`` a worktree card silently fell back to ``wt/<id>``.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools import kanban_tools as kt


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


def _stored(task_id):
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, task_id)


def _task_count():
    with kbc.connect_closing() as conn:
        return len(kb.list_tasks(conn))


def _create(**extra):
    return json.loads(kt._handle_create({"title": "card", "assignee": "peer", **extra}))


def test_create_worktree_card_persists_named_branch(kanban_home, tmp_path):
    worktree = str(tmp_path / "repo" / ".worktrees" / "slug")
    out = _create(workspace_kind="worktree", workspace_path=worktree, branch="  crew/slug ")
    assert out["ok"] is True
    assert (out["workspace_kind"], out["workspace_path"], out["branch_name"]) == (
        "worktree", worktree, "crew/slug")
    task = _stored(out["task_id"])
    assert (task.workspace_kind, task.workspace_path, task.branch_name) == (
        "worktree", worktree, "crew/slug")


@pytest.mark.parametrize("branch, message", [
    ("", "branch requires a non-empty name"),
    ("   ", "branch requires a non-empty name"),
    ("-evil", "branch must not start with '-'"),
    ("crew/two words", "branch must not contain whitespace"),
])
def test_create_rejects_invalid_branch_without_creating(kanban_home, branch, message):
    before = _task_count()
    out = _create(workspace_kind="worktree", branch=branch)
    assert out.get("ok") is not True
    assert message in out["error"]
    assert _task_count() == before


@pytest.mark.parametrize("ws", [{}, {"workspace_kind": "scratch"}, {"workspace_kind": "dir"}])
def test_create_rejects_branch_without_worktree(kanban_home, ws):
    before = _task_count()
    out = _create(branch="crew/slug", **ws)
    assert "branch is only valid with workspace_kind='worktree'" in out["error"]
    assert _task_count() == before


def test_create_without_branch_is_unchanged(kanban_home):
    out = _create(workspace_kind="worktree")
    assert out["ok"] is True and out["branch_name"] is None
    assert _stored(out["task_id"]).branch_name is None


def _cli_create(argv):
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    return kc._cmd_create(root.parse_args(["kanban", "create", *argv]))


def test_cli_branch_flag_keeps_its_messages_and_binding(kanban_home, capsys):
    assert _cli_create(["card", "--workspace", "worktree", "--branch=-x"]) == 2
    assert "kanban: --branch must not start with '-'" in capsys.readouterr().err
    assert _cli_create(["card", "--branch", "crew/slug"]) == 2
    assert "kanban: --branch is only valid with --workspace worktree" in capsys.readouterr().err
    assert _cli_create(["card", "--workspace", "worktree", "--branch", "crew/slug", "--json"]) == 0
    task_id = json.loads(capsys.readouterr().out)["id"]
    assert _stored(task_id).branch_name == "crew/slug"
