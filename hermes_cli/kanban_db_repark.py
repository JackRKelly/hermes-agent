"""Read the current park of a card and re-park a blocked card in place (action items, §4.1).

A re-park replaces the ask of a card that is already ``blocked``: one new ``blocked`` event with the
new reason, in the same transaction that proves the old park is still the current one. Going through
``unblock_task`` + ``block_task`` instead would leave the card ``ready`` between the two writes,
where the dispatcher could claim it, or ``todo`` (open parents), where ``block_task`` refuses.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

PARK_EVENTS = ("blocked", "block_loop_detected")


def latest_park_event(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    """The card's newest park: a ``blocked`` event, or the breaker's ``block_loop_detected``."""
    return conn.execute(
        "SELECT id, kind, payload, created_at, run_id FROM task_events WHERE task_id = ? "
        "AND kind IN (?, ?) ORDER BY id DESC LIMIT 1", (task_id, *PARK_EVENTS),
    ).fetchone()


def repark_blocked_task(
    conn: sqlite3.Connection, task_id: str, *, expected_park_event_id: int, reason: str,
    actor: str, kind: str = "needs_input",
) -> Optional[int]:
    """Append a new park on a ``blocked`` card whose current park is ``expected_park_event_id``.

    Status, ``block_kind`` and the loop counter are unchanged: it is the same wait with a new ask,
    not a new block. Returns the new event id, or None when the card is not blocked or another park
    landed first (compare-and-swap on the park event id).
    """
    from hermes_cli.kanban_db import _append_event, _fire_task_hook, _json_dict, get_task, write_txn

    with write_txn(conn):
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        current = latest_park_event(conn, task_id)
        if row is None or row["status"] != "blocked" or current is None \
                or int(current["id"]) != int(expected_park_event_id):
            return None
        # The phase the card resumes into on unblock (``review`` vs ``ready``) is read from the
        # newest park's ``source_status``, so the re-park carries the original one forward.
        source_status = _json_dict(current["payload"]).get("source_status") or "ready"
        _append_event(conn, task_id, "blocked", {
            "reason": reason, "kind": kind, "actor": actor, "source_status": source_status,
            "reparked_from": int(expected_park_event_id),
        })
        new_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        task = get_task(conn, task_id)
    _fire_task_hook("kanban_task_blocked", task, task_id, None, reason=reason)
    return new_id
