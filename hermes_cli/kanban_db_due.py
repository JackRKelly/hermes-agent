"""Optional task due dates: ``tasks.due_at`` (epoch seconds, NULL = no due date).

Most cards have no due date and that is normal: nothing here requires one, and an
absent date renders as nothing at all. A date is only ever set from an explicit
value (``hermes kanban create/edit --due`` or the ``kanban_create`` tool), never
inferred.

Input is ISO 8601 WITH a UTC offset (``2026-11-30T17:00:00-06:00`` or ``...Z``). A
naive timestamp is refused instead of being read in the host's zone: the board is
shared by profiles and the owner's zone is not the host's, so a guessed zone would
silently move a customer deadline by hours. The stored value is UTC epoch seconds;
display is in the owner's zone (:func:`owner_zone`), labelled CST/CDT, so the
offset an author typed and the wall time a reader sees always agree.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

# Words that clear a due date on ``hermes kanban edit --due``.
CLEAR_WORDS = frozenset({"none", "clear", "null", ""})

# The board owner's zone when the profile configures no ``timezone``. Deadlines on
# this board are written in Chicago time, and the host's zone (UTC under the test
# runner and in containers) would show a different wall time with no label.
OWNER_TZ_FALLBACK = "America/Chicago"


def owner_zone() -> ZoneInfo:
    """The zone due dates are shown in: the profile's ``timezone``, else Chicago."""
    import hermes_time

    return hermes_time.get_timezone() or ZoneInfo(OWNER_TZ_FALLBACK)


def format_due_at(due_at: int) -> str:
    """Absolute owner-zone time with its zone name: ``2026-11-30 17:00 CST``."""
    return datetime.fromtimestamp(int(due_at), owner_zone()).strftime("%Y-%m-%d %H:%M %Z")


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
        parsed = datetime.fromisoformat(raw)
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
