"""The create-time pinned-skill check resolves project skills from the card's workspace.

The worker chdirs into the card's workspace before its preload, so a trusted repo's
``.hermes/skills`` skill loads at run time. The create-time check must answer from that same
directory (not the creator's, not the Hermes checkout), and must not refuse when the workspace is
not settled until dispatch while the assignee trusts some project dir.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools import kanban_tools as kt


def _write_skill(skills_root: Path, name: str) -> None:
    skill_dir = skills_root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: fixture skill {name}\n---\n\n# {name}\n", encoding="utf-8")


@pytest.fixture
def project(monkeypatch, tmp_path):
    """A git repo carrying ``proj-only`` in its ``.hermes/skills``, and a ``reviewer`` profile."""
    default = tmp_path / ".hermes"
    reviewer = default / "profiles" / "reviewer"
    reviewer.mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _write_skill(repo / ".hermes" / "skills", "proj-only")
    monkeypatch.setenv("HERMES_HOME", str(default))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return reviewer, repo


def _trust(reviewer: Path, repo: Path | None) -> None:
    trusted = f"skills:\n  trusted_project_dirs:\n    - {repo}\n" if repo else "model: {}\n"
    (reviewer / "config.yaml").write_text(trusted, encoding="utf-8")


def _create(**extra):
    return json.loads(kt._handle_create({"title": "card", "assignee": "reviewer",
                                         "skills": ["proj-only"], **extra}))


def _task_count():
    with kbc.connect_closing() as conn:
        return len(kb.list_tasks(conn))


def test_trusted_project_skill_of_the_workspace_is_accepted(project):
    reviewer, repo = project
    _trust(reviewer, repo)
    out = _create(workspace_kind="dir", workspace_path=str(repo))
    assert out["ok"] is True, out


def test_untrusted_project_skill_of_the_workspace_is_refused(project):
    """The worker would not load an untrusted repo's skill either."""
    reviewer, repo = project
    _trust(reviewer, None)
    out = _create(workspace_kind="dir", workspace_path=str(repo))
    assert out.get("ok") is not True and "proj-only" in json.dumps(out)
    assert _task_count() == 0


def test_workspace_settled_at_dispatch_is_not_refused_when_projects_are_trusted(project):
    """A worktree's run-time dir is chosen at dispatch: a trusted project may supply the skill."""
    reviewer, repo = project
    _trust(reviewer, repo)
    assert _create(workspace_kind="worktree", workspace_path=str(repo))["ok"] is True
    assert _create()["ok"] is True


def test_missing_skill_is_refused_without_workspace_when_no_project_is_trusted(project):
    """No trusted project dir: the project tier is empty wherever the worker runs."""
    reviewer, _repo = project
    _trust(reviewer, None)
    out = _create()
    assert out.get("ok") is not True and "proj-only" in json.dumps(out)
