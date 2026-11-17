"""A ``scheduled`` card resumes the phase it was parked from.

A reviewer that schedules its run (waiting on CI) must come back in ``review``: a ``ready``
landing is re-claimed as an implementation run and ``request_changes`` then refuses it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _run_id(conn, tid):
    return kb.get_task(conn, tid).current_run_id


def _review_claimed(conn, *, parents=()):
    """A card whose live run was claimed from ``review`` by its reviewer."""
    tid = kb.create_task(conn, title="under review", assignee="worker", parents=list(parents))
    assert kb.claim_task(conn, tid) is not None
    assert kb.request_review(
        conn, tid, summary="v1", reviewer="reviewer", expected_run_id=_run_id(conn, tid),
    )
    assert kb.claim_review_task(conn, tid) is not None
    return tid


def _claimed_source(conn, tid):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'claimed' "
        "ORDER BY id DESC LIMIT 1", (tid,),
    ).fetchone()
    return json.loads(row["payload"] or "{}").get("source_status")


def test_scheduled_review_run_resumes_review_and_can_request_changes(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _review_claimed(conn)
        assert kb.schedule_task(conn, tid, reason="waiting on CI", expected_run_id=_run_id(conn, tid))

        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "review"

        assert kb.claim_review_task(conn, tid) is not None
        assert _claimed_source(conn, tid) == "review"
        ok, implementer = kb.request_changes(
            conn, tid, reason="fix the test", expected_run_id=_run_id(conn, tid),
        )
        assert (ok, implementer) == (True, "worker")


def test_scheduled_implementation_run_resumes_ready(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="implementing", assignee="worker")
        assert kb.claim_task(conn, tid) is not None
        assert kb.schedule_task(conn, tid, reason="waiting", expected_run_id=_run_id(conn, tid))

        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "ready"


def test_scheduled_review_run_with_open_parent_lands_todo(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _review_claimed(conn)
        assert kb.schedule_task(conn, tid, reason="waiting on CI", expected_run_id=_run_id(conn, tid))
        parent = kb.create_task(conn, title="new upstream", assignee="worker")
        kb.link_tasks(conn, parent, tid)

        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "todo"


def test_legacy_scheduled_event_falls_back_to_the_runs_claim(kanban_home: Path) -> None:
    """A ``scheduled`` event written before ``source_status`` was recorded."""
    with kbc.connect() as conn:
        tid = _review_claimed(conn)
        assert kb.schedule_task(conn, tid, reason="waiting on CI", expected_run_id=_run_id(conn, tid))
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE task_id = ? AND kind = 'scheduled'",
            (json.dumps({"reason": "waiting on CI"}), tid),
        )
        conn.commit()

        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "review"


def test_scheduled_from_a_review_block_resumes_review(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _review_claimed(conn)
        assert kb.block_task(
            conn, tid, reason="need a decision", kind="needs_input", expected_run_id=_run_id(conn, tid),
        )
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.schedule_task(conn, tid, reason="check back later")

        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "review"
