"""Idle-run reclaim: free the slot of a worker that is alive but making no progress.

``release_stale_claims`` / ``detect_stale_running`` judge a run by its heartbeat, and
``_touch_activity`` bridges any in-process activity into heartbeats, so a worker parked in one
long tool call (``gh pr checks --watch``) or a wedged loop looks alive forever and holds a
``max_in_progress`` slot.

Progress here is the worker's own conversation, not its heartbeat: the newest row the run wrote
to its session's ``messages`` table (every model turn and every tool result is persisted as it
happens), found through the run's ``owner_admitted`` receipt (``db`` + ``session_id``). The run's
start counts as progress too. A single tool call longer than the threshold writes no row, so it
counts as no progress. A run with no owner receipt has no readable activity source and is never
judged here.

After ``kanban.idle_reclaim_minutes`` (default 15, 0 disables) without progress the run gets an
``idle_flagged`` event; still idle ``kanban.idle_reclaim_grace_minutes`` (default 5) later, the
worker is terminated and the card requeued with run outcome ``reclaimed`` (event ``reclaimed``,
payload ``reason: idle``), never counted as a failure. Each idle reclaim feeds the block-loop
counters (``block_kind = 'idle_reclaim'``): the ``IDLE_RECLAIM_ESCALATE_AFTER``-th in a row
routes the card to ``triage`` with ``block_loop_detected`` instead of cycling it.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

DEFAULT_IDLE_RECLAIM_MINUTES = 15
DEFAULT_IDLE_RECLAIM_GRACE_MINUTES = 5
IDLE_RECLAIM_ESCALATE_AFTER = 3
IDLE_BLOCK_KIND = "idle_reclaim"


def _minutes(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def idle_settings() -> tuple[int, int]:
    """``(threshold_seconds, grace_seconds)``; threshold 0 disables idle reclaim."""
    from hermes_cli.config_backend import ConfigBackendUnavailable
    try:
        from hermes_cli.config import load_config
        section = (load_config() or {}).get("kanban") or {}
    except (ConfigBackendUnavailable, OSError, ValueError, TypeError, AttributeError):
        section = {}
    threshold = _minutes(section.get("idle_reclaim_minutes"), DEFAULT_IDLE_RECLAIM_MINUTES)
    grace = _minutes(section.get("idle_reclaim_grace_minutes"), DEFAULT_IDLE_RECLAIM_GRACE_MINUTES)
    return threshold * 60, grace * 60


def session_last_message_at(receipt: dict) -> Optional[float]:
    """Newest ``messages.timestamp`` of the run's session, read-only; None when unreadable."""
    try:
        uri = Path(receipt["db"]).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as db:
            row = db.execute("SELECT MAX(timestamp) FROM messages WHERE session_id = ?",
                             (receipt["session_id"],)).fetchone()
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error):
        return None
    return float(row[0]) if row and row[0] is not None else None


def last_progress_at(conn: sqlite3.Connection, task_id: str, run_started_at: Optional[int]) -> Optional[int]:
    """Epoch of the run's latest model turn or tool call (its start at the earliest), or None
    when the run has no owner receipt to read activity from."""
    from hermes_cli.kanban_owner_recovery import _owner_receipt
    receipt = _owner_receipt(conn, task_id)
    if not receipt or not receipt.get("session_id"):
        return None
    last = session_last_message_at(receipt)
    return int(max(last or 0, run_started_at or 0)) or None


def _flag_event(conn: sqlite3.Connection, task_id: str, run_id: int) -> Optional[tuple[int, dict]]:
    from hermes_cli import kanban_db as kb
    row = conn.execute("SELECT created_at, payload FROM task_events WHERE task_id = ? AND run_id = ? "
                       "AND kind = 'idle_flagged' ORDER BY id DESC LIMIT 1", (task_id, run_id)).fetchone()
    return (int(kb._to_epoch(row["created_at"]) or 0), kb._json_dict(row["payload"])) if row else None


def sweep_idle_runs(conn: sqlite3.Connection, *, now: Optional[int] = None, signal_fn=None) -> tuple[list[str], list[str]]:
    """Flag and reclaim idle host-local runs; returns ``(flagged, reclaimed)`` task ids."""
    threshold, grace = idle_settings()
    if threshold <= 0:
        return [], []
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_owner_recovery import owner_reclaim_blocked
    now = int(time.time()) if now is None else int(now)
    flagged: list[str] = []
    reclaimed: list[str] = []
    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, t.claim_lock, t.current_run_id, t.assignee, "
        "r.started_at AS run_started_at FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.worker_pid IS NOT NULL").fetchall()
    for row in rows:
        if not str(row["claim_lock"] or "").startswith(kb._host_prefix()) or owner_reclaim_blocked(conn, row["id"]):
            continue
        progress = last_progress_at(conn, row["id"], kb._to_epoch(row["run_started_at"]))
        if progress is None or now - progress < threshold:
            continue
        flag = _flag_event(conn, row["id"], row["current_run_id"])
        if flag is None or flag[1].get("last_progress_at") != progress:
            with kb.write_txn(conn):
                kb._append_event(conn, row["id"], "idle_flagged", {
                    "idle_seconds": now - progress, "last_progress_at": progress,
                    "threshold_seconds": threshold, "grace_seconds": grace,
                    "source": "session_messages"}, run_id=row["current_run_id"])
            flagged.append(row["id"])
            continue
        if now - flag[0] >= grace and _reclaim_idle_run(conn, row, progress, now, signal_fn=signal_fn):
            reclaimed.append(row["id"])
    return flagged, reclaimed


def _reclaim_idle_run(conn: sqlite3.Connection, row, progress: int, now: int, *, signal_fn=None) -> bool:
    """Terminate the idle worker, requeue (or escalate) the card. Mirrors ``enforce_max_runtime``."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli.kanban_owner_recovery import owner_reclaim_blocked_after_kill
    tid, lock = row["id"], row["claim_lock"]
    termination = kb._terminate_reclaimed_worker(row["worker_pid"], lock, signal_fn=signal_fn,
                                                 started_at=kb._row_get(row, "worker_started_at"))
    if kbd._worker_survived_termination(termination):
        kbd._defer_reclaim_for_live_worker(conn, tid, lock, now, termination, reason="idle_worker_alive")
        return False
    with kb.write_txn(conn):
        if owner_reclaim_blocked_after_kill(conn, tid, signalled=bool(termination.get("termination_attempted"))):
            return False
        prev = conn.execute("SELECT block_kind, block_recurrences FROM tasks WHERE id = ?", (tid,)).fetchone()
        recurrences = (int(prev["block_recurrences"] or 0) + 1) if prev and prev["block_kind"] == IDLE_BLOCK_KIND else 1
        escalate = recurrences >= IDLE_RECLAIM_ESCALATE_AFTER
        status = "triage" if escalate else kb._retry_status_for_run(conn, tid)
        cur = conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
            "worker_started_at = NULL, last_heartbeat_at = NULL, block_kind = ?, block_recurrences = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock IS ?",
            (status, IDLE_BLOCK_KIND, recurrences, tid, lock))
        if cur.rowcount != 1:
            return False
        payload = {"reason": "idle", "idle_seconds": now - progress, "last_progress_at": progress,
                   "idle_reclaims": recurrences, "retry_status": status, **termination}
        run_id = kb._end_run(conn, tid, outcome="reclaimed", status="reclaimed",
                             error=f"idle: no model turn or tool call for {now - progress}s", metadata=payload)
        kb._append_event(conn, tid, "reclaimed", payload, run_id=run_id)
        if escalate:
            kb._append_event(conn, tid, "block_loop_detected", {
                "reason": f"reclaimed idle {recurrences} times in a row", "kind": IDLE_BLOCK_KIND,
                "recurrences": recurrences, "limit": IDLE_RECLAIM_ESCALATE_AFTER, "source_status": "running"},
                run_id=run_id)
    log.warning("kanban: reclaimed idle run of %s (%ss without progress)%s", tid, now - progress,
                ", escalated to triage" if escalate else "")
    return True


def recent_idle(conn: sqlite3.Connection, *, since: int) -> list[dict]:
    """Health rows: the latest idle flag/reclaim of each run since ``since``."""
    from hermes_cli import kanban_db as kb
    out: dict[tuple[str, int], dict] = {}
    rows = conn.execute(
        "SELECT e.task_id, e.run_id, e.kind, e.payload, e.created_at, r.profile FROM task_events e "
        "LEFT JOIN task_runs r ON r.id = e.run_id WHERE e.created_at >= ? AND (e.kind = 'idle_flagged' "
        "OR (e.kind = 'reclaimed' AND e.payload LIKE '%\"reason\": \"idle\"%')) ORDER BY e.id", (since,)).fetchall()
    for r in rows:
        payload = kb._json_dict(r["payload"])
        out[(r["task_id"], int(r["run_id"] or 0))] = {
            "task_id": r["task_id"], "run_id": int(r["run_id"] or 0), "profile": r["profile"] or "",
            "idle_seconds": int(payload.get("idle_seconds") or 0),
            "state": "flagged" if r["kind"] == "idle_flagged" else "reclaimed",
            "at": int(kb._to_epoch(r["created_at"]) or 0)}
    return list(out.values())
