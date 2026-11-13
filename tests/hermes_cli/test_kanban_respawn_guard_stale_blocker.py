"""A quota/auth stamp left by an older run must not park a card whose latest run handed off (#119070).

The rate-limit requeue stamps ``tasks.last_failure_error`` with quota text and nothing on the
handoff paths clears it, so ``check_respawn_guard`` must judge the stamp by the latest ended run.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

QUOTA_STAMP = "pid 31350 exited rate-limited (quota wall) — requeued without counting a failure"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    kb.init_db()
    return home


def _rate_limited_then_review(conn) -> str:
    """Implementer run 1 hits the quota wall (requeued, stamped); run 2 requests review."""
    tid = kb.create_task(conn, title="rl then review", assignee="builder")
    kb.claim_task(conn, tid)
    rl_run = kb.get_task(conn, tid).current_run_id
    conn.execute(
        "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', ended_at=? WHERE id=?",
        (int(time.time()) - 1000, rl_run),
    )
    conn.execute(
        "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, claim_expires=NULL, "
        "worker_pid=NULL, last_failure_error=? WHERE id=?",
        (QUOTA_STAMP, tid),
    )
    conn.commit()
    kb.claim_task(conn, tid)
    run = kb.get_task(conn, tid).current_run_id
    assert kb.request_review(conn, tid, summary="ready", reviewer="reviewer", expected_run_id=run)
    return tid


def test_review_handoff_after_rate_limited_run_spawns_the_reviewer(kanban_home):
    with kbc.connect() as conn:
        tid = _rate_limited_then_review(conn)
        task = kb.get_task(conn, tid)
        # Preconditions: the card sits in review with the old quota stamp still on it.
        assert (task.status, task.assignee) == ("review", "reviewer")
        assert task.last_failure_error == QUOTA_STAMP
        assert kbd.check_respawn_guard(conn, tid, lane="review") is None


def test_quota_failure_after_the_handoff_still_guards(kanban_home):
    """A failure run that is itself the latest keeps the blocker: the stamp is its own diagnosis."""
    with kbc.connect() as conn:
        tid = _rate_limited_then_review(conn)
        now = int(time.time())
        with kb.write_txn(conn):
            # The reviewer run times out on a quota wall; ended_at strictly after the handoff run
            # so the "latest run" query deterministically picks it.
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
                "VALUES (?, 'reviewer', 'timed_out', 'timed_out', ?, ?)",
                (tid, now, now + 5),
            )
            conn.execute("UPDATE tasks SET last_failure_error=? WHERE id=?",
                         ("provider returned 429: quota exceeded", tid))
        assert kbd.check_respawn_guard(conn, tid, lane="review") == "blocker_auth"
