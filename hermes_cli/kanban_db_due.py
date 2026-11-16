"""Optional task due dates: ``tasks.due_at`` (epoch seconds, NULL = no due date).

Most cards have no due date and that is normal: nothing here requires one, and an
absent date renders as nothing at all. A date is only ever set from an explicit
value (``--due``, the ``kanban_create`` tool, the dashboard API), never inferred.

Input is ISO 8601 WITH a UTC offset (``2026-11-30T17:00:00-06:00`` or ``...Z``). A
naive timestamp is refused instead of being read in the host's zone: the board is
shared by profiles and the owner's zone is not the host's, so a guessed zone would
silently move a customer deadline by hours.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from typing import Optional

# Words that clear a due date on ``hermes kanban edit --due``.
CLEAR_WORDS = frozenset({"none", "clear", "null", ""})


def parse_due(text: Optional[str]) -> Optional[int]:
    """ISO 8601 with an offset -> epoch seconds. ``None`` passes through (not given).

    Raises ``ValueError`` for an unparseable value or one without a UTC offset.
    A clear word is not accepted here; callers that allow clearing check
    :data:`CLEAR_WORDS` first.
    """
    if text is None:
        return None
    raw = str(text).strip()
    try:
        # ``fromisoformat`` on Python < 3.11 rejects a trailing ``Z``.
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw)
    except ValueError as exc:
        raise ValueError(f"not an ISO 8601 date-time: {raw!r} (e.g. 2026-11-30T17:00:00-06:00)") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"due date needs a UTC offset: {raw!r} (e.g. 2026-11-30T17:00:00-06:00 or ...Z)")
    return int(parsed.timestamp())


def _span(seconds: int) -> str:
    """Coarse human span: ``13d``, ``5h``, ``20m`` (largest unit, floored, never 0d)."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return "<1m"


def format_due(due_at: Optional[int], now: Optional[float] = None) -> str:
    """Relative label for a due date: ``due in 13d`` / ``overdue 2h``; ``""`` when unset."""
    if due_at is None:
        return ""
    delta = int(due_at) - int(now if now is not None else time.time())
    return f"due in {_span(delta)}" if delta >= 0 else f"overdue {_span(-delta)}"


def set_due_at(
    conn: sqlite3.Connection, task_id: str, due_at: Optional[int], *, board: Optional[str] = None,
) -> bool:
    """Set (or clear with ``None``) a task's due date; False for an unknown id.

    Records an ``edited`` event carrying the new value so the board history shows
    when and to what a deadline moved.
    """
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        if kb._task_status(conn, task_id) is None:
            return False
        conn.execute("UPDATE tasks SET due_at = ? WHERE id = ?", (due_at, task_id))
        kb._append_event(conn, task_id, "edited", {"fields": ["due_at"], "due_at": due_at})
    kb.notify_task_updated(conn, task_id, ["due_at"], board=board)
    return True
