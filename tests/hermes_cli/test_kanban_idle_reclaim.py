"""Idle-run reclaim: a live worker with no model turn and no tool call for
``kanban.idle_reclaim_minutes`` is flagged, then reclaimed after the grace (requeued, never a
failure); the third idle reclaim in a row escalates to triage. Heartbeats are not progress: the
activity source is the run's own session ``messages`` rows.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_idle as kbi
from hermes_cli.process_identity import worker_birth

MIN = 60


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)  # the signalled worker is gone
    kb.init_db()
    state = tmp_path / "state.db"
    with closing(sqlite3.connect(state)) as db:
        db.execute("CREATE TABLE messages (session_id TEXT, timestamp REAL)")
        db.execute("CREATE TABLE session_admissions (target_session_id TEXT, request_id TEXT, "
                   "status TEXT, outcome TEXT)")
        db.commit()
    with kbc.connect_closing() as conn:
        yield conn, state


def _start_run(conn, state: Path, tid: str, *, session: str) -> int:
    host = kb._claimer_id().split(":", 1)[0]
    claimed = kb.claim_task(conn, tid, claimer=f"{host}:w")
    assert claimed is not None
    conn.execute("UPDATE tasks SET worker_pid = 99991, worker_started_at = NULL WHERE id = ?", (tid,))
    request = f"req-{claimed.current_run_id}"
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "owner_admitted", {
            "db": str(state), "session_id": session, "request_id": request,
            "pid": os.getpid(), "birth": worker_birth(psutil.Process(os.getpid()))},
            run_id=claimed.current_run_id)
    with closing(sqlite3.connect(state)) as db:
        db.execute("INSERT INTO session_admissions VALUES (?, ?, 'running', NULL)", (session, request))
        db.commit()
    return claimed.current_run_id


def _message(state: Path, session: str, at: float) -> None:
    with closing(sqlite3.connect(state)) as db:
        db.execute("INSERT INTO messages VALUES (?, ?)", (session, at))
        db.commit()


def _events(conn, tid, kind):
    return [json.loads(r["payload"] or "{}") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind))]


def test_flagged_at_threshold_then_reclaimed_after_grace_and_requeued(board):
    conn, state = board
    tid = kb.create_task(conn, title="idle", assignee="a")
    run_id = _start_run(conn, state, tid, session="s1")
    t = int(time.time())
    _message(state, "s1", t)

    assert kbi.sweep_idle_runs(conn, now=t + 14 * MIN) == ([], [])
    assert kbi.sweep_idle_runs(conn, now=t + 15 * MIN) == ([tid], [])
    assert _events(conn, tid, "idle_flagged")[0]["source"] == "session_messages"
    assert kb.get_task(conn, tid).status == "running"

    assert kbi.sweep_idle_runs(conn, now=t + 21 * MIN) == ([], [tid])

    task = kb.get_task(conn, tid)
    assert task.status == "ready" and task.consecutive_failures == 0 and task.worker_pid is None
    run = conn.execute("SELECT outcome FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["outcome"] == "reclaimed"
    assert _events(conn, tid, "reclaimed")[-1]["reason"] == "idle"


def test_fresh_tool_activity_is_never_flagged(board):
    conn, state = board
    tid = kb.create_task(conn, title="busy", assignee="a")
    _start_run(conn, state, tid, session="s1")
    t = int(time.time())
    for minute in range(0, 60, 5):
        _message(state, "s1", t + minute * MIN)
        assert kbi.sweep_idle_runs(conn, now=t + minute * MIN + 4 * MIN) == ([], [])
    assert not _events(conn, tid, "idle_flagged")


def test_activity_after_flag_cancels_reclaim(board):
    conn, state = board
    tid = kb.create_task(conn, title="woke", assignee="a")
    _start_run(conn, state, tid, session="s1")
    t = int(time.time())
    _message(state, "s1", t)
    assert kbi.sweep_idle_runs(conn, now=t + 16 * MIN)[0] == [tid]
    _message(state, "s1", t + 17 * MIN)
    assert kbi.sweep_idle_runs(conn, now=t + 22 * MIN) == ([], [])
    assert kb.get_task(conn, tid).status == "running"


def test_three_idle_reclaims_escalate_via_block_loop(board):
    conn, state = board
    tid = kb.create_task(conn, title="wedged", assignee="a")
    for attempt in range(1, 4):
        _start_run(conn, state, tid, session=f"s{attempt}")
        t = int(time.time())
        assert kbi.sweep_idle_runs(conn, now=t + 16 * MIN)[0] == [tid]
        assert kbi.sweep_idle_runs(conn, now=t + 22 * MIN)[1] == [tid]
        if attempt < 3:
            assert kb.get_task(conn, tid).status == "ready"
    task = kb.get_task(conn, tid)
    assert task.status == "triage" and task.consecutive_failures == 0
    loop = _events(conn, tid, "block_loop_detected")
    assert loop and loop[-1]["kind"] == kbi.IDLE_BLOCK_KIND and loop[-1]["recurrences"] == 3


def test_zero_disables(board, monkeypatch):
    conn, state = board
    monkeypatch.setattr(kbi, "idle_settings", lambda: (0, 300))
    tid = kb.create_task(conn, title="idle", assignee="a")
    _start_run(conn, state, tid, session="s1")
    assert kbi.sweep_idle_runs(conn, now=int(time.time()) + 600 * MIN) == ([], [])


def test_config_keys_drive_threshold(board, tmp_path):
    (tmp_path / ".hermes" / "config.yaml").write_text(
        "kanban:\n  idle_reclaim_minutes: 2\n  idle_reclaim_grace_minutes: 1\n")
    assert kbi.idle_settings() == (120, 60)
    (tmp_path / ".hermes" / "config.yaml").write_text("kanban:\n  idle_reclaim_minutes: 0\n")
    assert kbi.idle_settings()[0] == 0


def test_dispatch_tick_runs_the_idle_sweep(board, monkeypatch):
    conn, state = board
    tid = kb.create_task(conn, title="idle", assignee="a")
    _start_run(conn, state, tid, session="s1")
    monkeypatch.setattr(kbd, "_worker_alive", lambda pid, started_at: True)  # the crash sweep sees it live
    monkeypatch.setattr(kbi, "idle_settings", lambda: (1, 0))
    _message(state, "s1", time.time() - 120)
    conn.execute("UPDATE task_runs SET started_at = ? WHERE task_id = ?", (int(time.time()) - 120, tid))
    kbd.dispatch_once(conn, dry_run=True)
    assert _events(conn, tid, "idle_flagged")
