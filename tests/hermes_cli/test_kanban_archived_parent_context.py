"""A child's worker context keeps the handoff of a parent archived after it completed."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_archived_done_parent_still_hands_off(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="builder", assignee="worker")
        kb.complete_task(conn, parent, summary="PR #12 at abc123 approved")
        child = kb.create_task(conn, title="merge", assignee="worker", parents=(parent,))
        assert kb.archive_task(conn, parent)
        assert kb.get_task(conn, parent).status == "archived"

        ctx = kb.build_worker_context(conn, child)

        assert "## Parent task results" in ctx
        assert f"### {parent}" in ctx
        assert "PR #12 at abc123 approved" in ctx


def test_parent_archived_without_completing_gives_no_handoff(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="abandoned", assignee="worker")
        child = kb.create_task(conn, title="follower", assignee="worker", parents=(parent,))
        assert kb.archive_task(conn, parent)

        ctx = kb.build_worker_context(conn, child)

        assert "## Parent task results" not in ctx
