"""A card pinning a skill its assignee profile cannot load is refused at creation.

The skill resolves under the ASSIGNEE's home (its skills dir, ``skills.external_dirs``, disabled
list), not the creator's: a reviewer card pinned to a skill that only the default profile carried
crash-looped on every run (the worker's preload found nothing).
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


def _write_skill(root: Path, name: str) -> None:
    skill_dir = root / "skills" / "testing" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: fixture skill {name}\n---\n\n# {name}\n", encoding="utf-8")


@pytest.fixture
def homes(monkeypatch, tmp_path):
    """Default home with a skill only it carries, and a ``reviewer`` profile without it."""
    default = tmp_path / ".hermes"
    reviewer = default / "profiles" / "reviewer"
    reviewer.mkdir(parents=True)
    (reviewer / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    _write_skill(default, "default-only-review")
    _write_skill(reviewer, "reviewer-own")
    monkeypatch.setenv("HERMES_HOME", str(default))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return default, reviewer


def _task_count():
    with kbc.connect_closing() as conn:
        return len(kb.list_tasks(conn))


def _tool_create(**extra):
    return json.loads(kt._handle_create({"title": "card", **extra}))


def test_tool_refuses_skill_only_the_creator_has(homes):
    out = _tool_create(assignee="reviewer", skills=["default-only-review"])
    assert out.get("ok") is not True
    message = json.dumps(out)
    assert "default-only-review" in message and "reviewer" in message
    assert _task_count() == 0


def test_tool_accepts_skill_the_assignee_has(homes):
    out = _tool_create(assignee="reviewer", skills=["reviewer-own"])
    assert out["ok"] is True
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, out["task_id"]).skills == ["reviewer-own"]


def test_external_dirs_of_the_assignee_count(homes, tmp_path):
    _default, reviewer = homes
    shared = tmp_path / "shared"
    _write_skill(shared, "shared-review")
    (reviewer / "config.yaml").write_text(
        f"skills:\n  external_dirs:\n    - {shared / 'skills'}\n", encoding="utf-8")
    assert _tool_create(assignee="reviewer", skills=["shared-review"])["ok"] is True


def test_skill_disabled_for_the_assignee_is_refused(homes):
    _default, reviewer = homes
    (reviewer / "config.yaml").write_text("skills:\n  disabled:\n    - reviewer-own\n", encoding="utf-8")
    out = _tool_create(assignee="reviewer", skills=["reviewer-own"])
    assert out.get("ok") is not True and "reviewer-own" in json.dumps(out)
    assert _task_count() == 0


def test_unknown_assignee_is_left_to_the_dispatcher(homes):
    """No home to resolve against: the dispatcher's own unknown-profile handling applies."""
    assert _tool_create(assignee="nobody-here", skills=["default-only-review"])["ok"] is True


def _cli_args(**overrides):
    base = dict(title="card", body=None, body_file=None, assignee="reviewer", created_by=None,
                workspace="scratch", branch=None, project=None, tenant=None, priority=0, parent=[],
                triage=False, idempotency_key=None, max_runtime=None, skills=[], max_retries=None,
                model_override=None, provider_override=None, goal_mode=False, goal_max_turns=None,
                completion_contract=None, due=None, initial_status="running", json=True)
    return argparse.Namespace(**{**base, **overrides})


def test_cli_create_refuses_missing_pinned_skill(homes, capsys):
    assert kc._cmd_create(_cli_args(skills=["default-only-review"])) == 2
    err = capsys.readouterr().err
    assert "default-only-review" in err and "reviewer" in err
    assert _task_count() == 0


def test_cli_create_accepts_installed_pinned_skill(homes, capsys):
    assert kc._cmd_create(_cli_args(skills=["reviewer-own"])) == 0
    assert _task_count() == 1
