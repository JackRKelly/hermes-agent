"""A quota/auth stamp left by an older run must not park a card whose later runs did not fail on it (#119070).

The rate-limit requeue stamps ``tasks.last_failure_error`` with quota text, and nothing on the handoff,
stale-claim or reclaim paths rewrites it, so ``check_respawn_guard`` must judge the latest ended run's
own error, not the column.
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
        "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', ended_at=?, error=? WHERE id=?",
        (int(time.time()) - 1000, QUOTA_STAMP, rl_run),
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


def _reviewer_run_ends(conn, tid: str, outcome: str, error: str) -> None:
    """A reviewer run ends after the handoff; ended_at strictly later so it is the latest run."""
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, error) "
            "VALUES (?, 'reviewer', ?, ?, ?, ?, ?)",
            (tid, outcome, outcome, now, now + 5, error),
        )


def test_review_handoff_after_rate_limited_run_spawns_the_reviewer(kanban_home):
    with kbc.connect() as conn:
        tid = _rate_limited_then_review(conn)
        task = kb.get_task(conn, tid)
        # Preconditions: the card sits in review with the old quota stamp still on it.
        assert (task.status, task.assignee) == ("review", "reviewer")
        assert task.last_failure_error == QUOTA_STAMP
        assert kbd.check_respawn_guard(conn, tid, lane="review") is None


@pytest.mark.parametrize("outcome,error", [
    # The texts ``release_stale_claims`` and ``reconcile_orphaned_running`` write; neither touches the column.
    ("stale", "no heartbeat ever after 4000s running"),
    ("reclaimed", "orphaned running card (broken claim bookkeeping)"),
])
def test_unrelated_failure_after_the_handoff_does_not_revive_the_stale_stamp(kanban_home, outcome, error):
    with kbc.connect() as conn:
        tid = _rate_limited_then_review(conn)
        _reviewer_run_ends(conn, tid, outcome, error)
        task = kb.get_task(conn, tid)
        assert task is not None and task.last_failure_error == QUOTA_STAMP
        assert kbd.check_respawn_guard(conn, tid, lane="review") is None


def test_quota_failure_after_the_handoff_still_guards(kanban_home):
    """A latest run that itself failed on quota keeps the blocker: the error is its own diagnosis."""
    with kbc.connect() as conn:
        tid = _rate_limited_then_review(conn)
        quota = "provider returned 429: quota exceeded"
        _reviewer_run_ends(conn, tid, "timed_out", quota)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET last_failure_error=? WHERE id=?", (quota, tid))
        assert kbd.check_respawn_guard(conn, tid, lane="review") == "blocker_auth"
