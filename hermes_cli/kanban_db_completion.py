"""Canonical card completion, split from the Kanban facade.

Facade helpers stay late-bound so existing owner/test policy replacements apply.
"""
import sqlite3
import time
from typing import Iterable, Optional


def complete_task(
    conn: sqlite3.Connection, task_id: str, *, result: Optional[str] = None,
    summary: Optional[str] = None, metadata: Optional[dict] = None,
    created_cards: Optional[Iterable[str]] = None, expected_run_id: Optional[int] = None,
    fire_lifecycle_hook: bool = True, force: bool = False,
    _completion_guard=None,
) -> bool:
    """``running|ready|blocked|review -> done``; records ``result``.

    ``ready`` is accepted for manual CLI completion, ``review`` for human
    approval. A ``running`` task under a live claim is only completed with
    proof of ownership (``expected_run_id``) or ``force=True`` (explicit
    operator override) — otherwise :class:`LiveClaimError`, the same fence
    :func:`request_review` applies. With no active run the handoff fields survive via
    :func:`_synthesize_ended_run`. ``summary`` (defaults to ``result``) and
    ``metadata`` land on the closing run for :func:`build_worker_context`.
    ``created_cards`` are verified first — a phantom id raises
    :class:`HallucinatedCardsError` after an auditable event; afterwards the
    prose is scanned for unresolvable ``t_<hex>`` refs (advisory event only).
    Completions from non-review statuses need evidence: a stripped ``result``
    or ``summary``, or a stripped result already stored on the card. Empty or
    whitespace-only evidence raises :class:`EmptyCompletionError` after an
    auditable event. Approving a card out of ``review`` stays exempt.
    """
    now = int(time.time())
    # Cheap pre-check; re-checked inside the txn to close the parent-reopen race.
    if not _kb._parents_satisfied(conn, task_id):
        return False
    from hermes_cli.kanban_pr_acceptance_store import prepare_acceptance, record_acceptance
    verified_cards = _kb._gate_created_cards(conn, task_id, created_cards, summary or result)
    _kb._gate_empty_completion(conn, task_id, result=result, summary=summary)
    metadata = _kb._merge_completion_prose_artifacts(
        conn, task_id, metadata, summary=summary, result=result,
    )
    handoff_summary = summary if summary is not None else result
    acceptance = prepare_acceptance(conn, task_id, expected_run_id, metadata)
    if acceptance is False:
        return False
    with _kb.write_txn(conn):
        # Hard invariant even for human review approval: a parent may have
        # reopened while this task waited.
        if not _kb._parents_satisfied(conn, task_id):
            return False
        if acceptance is not None and not record_acceptance(conn, task_id, acceptance):
            return False
        trow = conn.execute(
            "SELECT status, claim_lock, worker_pid, worker_started_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        prior_status = trow["status"] if trow else None
        # Refuse to close a LIVE worker's run without proof of ownership
        # (expected_run_id) or an explicit human override (force=True); see
        # _claim_is_live for what "live" means.
        if expected_run_id is None and not force and trow and _kb._claim_is_live(trow):
            raise _kb.LiveClaimError(task_id)
        # Trusted owner-only completion precondition under this card's writer
        # lock. It must not commit this connection or run arbitrary hooks.
        if _completion_guard is not None:
            _completion_guard(conn)
        sql = """
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL,
                       block_kind   = NULL,
                       block_recurrences = 0
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked', 'review')
                """
        params: tuple = (result, now, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params = (*params, int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return False
        if isinstance(metadata, dict):
            _kb._stage_completion_artifacts(conn, task_id, metadata, now)
        run_id = _kb._end_run(
            conn, task_id, outcome="completed", status="done", summary=handoff_summary,
            metadata=metadata,
        )
        # Never-claimed task: synthesize a run so the handoff fields survive.
        if run_id is None and (summary or metadata or result or prior_status == "review"):
            synth_summary, synth_metadata = handoff_summary, metadata
            if prior_status == "review" and not synth_summary and not synth_metadata:
                synth_summary = _kb._REVIEW_APPROVED_NOTE
                synth_metadata = {"source_status": "review", "approval": "manual"}
            run_id = _kb._synthesize_ended_run(
                conn, task_id, outcome="completed", summary=synth_summary, metadata=synth_metadata,
            )
        event_summary = handoff_summary
        if prior_status == "review" and not event_summary:
            event_summary = _kb._REVIEW_APPROVED_NOTE
        _kb._append_event(
            conn, task_id, "completed",
            _kb._completed_event_payload(result, event_summary, verified_cards, metadata),
            run_id=run_id,
        )
    _kb._flag_phantom_prose_refs(conn, task_id, run_id, summary, result, verified_cards)
    # Success wipes the breaker counter (history stays on the event log).
    _kb._clear_failure_counter(conn, task_id)
    _kb.recompute_ready(conn)  # separate txn so children see ``done``
    _kb._cleanup_workspace(conn, task_id)
    _done_task = _kb.get_task(conn, task_id)
    if fire_lifecycle_hook:
        _kb._fire_task_hook("kanban_task_completed", _done_task, task_id, run_id, summary=handoff_summary)
    return True


from hermes_cli import kanban_db as _kb
