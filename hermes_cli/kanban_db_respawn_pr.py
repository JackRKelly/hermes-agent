"""Which GitHub PRs cited in a card's comments are the card's OWN, for the ``active_pr`` respawn guard.

Comments routinely cite other cards' PRs (a hold request, a merge gate, a follow-up), so a raw
PR URL is not evidence that this card's worker opened a PR. Ownership is first mention: a PR is
foreign when another card's comment cites it strictly before this card's earliest comment that
does. Only comments count (bodies can be rewritten, their times are not evidence). Every doubt —
same-second tie, unreadable time — keeps the PR owned, so the duplicate-PR guard fails closed.
"""

from __future__ import annotations

import re
import sqlite3

PR_URL_RE = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)", re.IGNORECASE)


def _pr_keys(body: str) -> set[tuple[str, str, int]]:
    return {(owner.lower(), repo.lower(), int(num)) for owner, repo, num in PR_URL_RE.findall(body)}


def _mention_times(conn: sqlite3.Connection, task_id: str, key: tuple[str, str, int], *, own: bool):
    """Parsed times (None if unreadable) of comments citing ``key`` on (``own``) or off this card."""
    from hermes_cli import kanban_db as _kb

    sql = f"SELECT created_at, body FROM task_comments WHERE task_id {'=' if own else '!='} ? AND body LIKE ?"
    for created_at, body in conn.execute(sql, (task_id, f"%/pull/{key[2]}%")):
        if key in _pr_keys(_kb._lossy_text(body) or ""):
            yield _kb._to_epoch(created_at)


def _is_foreign(conn: sqlite3.Connection, task_id: str, key: tuple[str, str, int]) -> bool:
    own = list(_mention_times(conn, task_id, key, own=True))
    if not own or None in own:
        return False
    first = min(own)
    return any(t is not None and t < first for t in _mention_times(conn, task_id, key, own=False))


def cites_own_pr(conn: sqlite3.Connection, task_id: str, body: str) -> bool:
    """True when ``body`` cites at least one PR this card owns (see module docstring)."""
    return any(not _is_foreign(conn, task_id, key) for key in _pr_keys(body))
