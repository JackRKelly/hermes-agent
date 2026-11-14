"""Where a ``scheduled`` card resumes: the phase it was parked from, not always ``ready``.

``schedule_task`` records that phase as ``source_status`` on the ``scheduled`` event, and
``unblock_task`` restores it. Without it a reviewer that parks a run claimed from ``review``
(waiting on CI) came back ``ready``, was re-claimed as an implementation run, and
``request_changes`` refused it ("active run was not claimed from review").
"""
from __future__ import annotations

import sqlite3


def schedule_source_status(conn: sqlite3.Connection, task_id: str) -> str:
    """The resumable phase of a card about to be scheduled; call in the write txn, before
    the UPDATE: a ``running`` card's run claim, a ``blocked`` card's resume status, else ``ready``."""
    from hermes_cli import kanban_db as kb

    row = conn.execute(
        "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    status = row["status"] if row else None
    if status == "running":
        return kb._retry_status_for_run(conn, task_id, row["current_run_id"])
    if status == "blocked":
        return kb._resume_status_from_events(conn, task_id)
    return "ready"


def _scheduled_resume_status(conn: sqlite3.Connection, task_id: str) -> str:
    """``source_status`` of the newest ``scheduled`` event; for events written before it was
    recorded, the ``claimed`` event of the run that event closed; else ``ready``."""
    from hermes_cli import kanban_db as kb

    event = conn.execute(
        "SELECT payload, run_id FROM task_events WHERE task_id = ? AND kind = 'scheduled' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if event is None:
        return "ready"
    recorded = kb._json_dict(event["payload"]).get("source_status")
    if recorded is not None:
        return "review" if recorded == "review" else "ready"
    if event["run_id"] is None:
        return "ready"
    return kb._retry_status_for_run(conn, task_id, event["run_id"])


def unblock_resume_status(conn: sqlite3.Connection, task_id: str) -> str:
    """Phase ``unblock_task`` restores before the parent re-gate: ``blocked`` from its
    lifecycle events, ``scheduled`` from its ``scheduled`` event, anything else ``ready``."""
    from hermes_cli import kanban_db as kb

    status = kb._task_status(conn, task_id)
    if status == "blocked":
        return kb._resume_status_from_events(conn, task_id)
    if status == "scheduled":
        return _scheduled_resume_status(conn, task_id)
    return "ready"
