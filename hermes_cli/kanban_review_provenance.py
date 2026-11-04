"""Canonical re-review routing from the existing changes_requested event."""
import sqlite3


def prior_reviewer(conn: sqlite3.Connection, task_id: str):
    """None for the first review; False for missing/malformed prior provenance."""
    from hermes_cli import kanban_db as kb
    changes_run = conn.execute(
        "SELECT id FROM task_runs "
        "WHERE task_id = ? AND outcome = 'changes_requested' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if changes_run is None:
        return None
    changes_event = kb._latest_event(conn, task_id, 'changes_requested', changes_run['id'])
    reviewer = kb._json_dict(kb._row_get(changes_event, 'payload')).get('reviewer')
    return reviewer if isinstance(reviewer, str) and reviewer.strip() else False
