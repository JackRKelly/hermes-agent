"""Board writes behind the ``action_item_*`` tools: comment-then-block, re-park, withdraw (§4.1).

Order of every write: the ``[action-item:v1]`` comment first, then the park whose reason carries
``item=<hash16>``. If the park fails or the run dies between the two, the comment is an orphan that
no park binds, so it is never shown (§3.1). Everything is validated before the first write: an
invalid call writes nothing.

Identity is never an argument. ``requested_by`` is the calling profile (``current_profile_name``),
stamped as the comment author and as the park event's ``actor``; the card is the dispatcher task
(``$HERMES_KANBAN_TASK``) or an explicit card the caller owns (its assignee or creator). No path
here can write a decision (``[jack-approved`` / ``[jack-rejected`` / ``[jack-done``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from agent.redact import redact_sensitive_text
from tools.action_item_kinds.core import (
    WITHDRAWN_PREFIX, SchemaError, canonical_json, check_text, comment_body, comment_hash16,
    reason_item_hash, refuse_decision_text,
)
from tools.action_item_kinds.registry import build_envelope, park_reason, spec_for
from tools.action_item_kinds.spec import LiveSources
from tools.action_item_view import (
    ItemRefused, ItemView, binding_problems, item_id, load_item, parse_item_id,
)

ADDABLE_STATUSES = ("running", "ready")
# E.164 handles are payload data the redactor would mask; mask them first so the secret check
# looks only at what remains.
_PHONE_RE = re.compile(r"\+[1-9][0-9]{6,14}")
ENVELOPE_ARGS = ("title", "summary", "ask", "why", "risk", "deadline", "links", "then", "batchable")


@dataclass(frozen=True)
class Caller:
    profile: str
    env_task: Optional[str]  # the dispatcher task this session owns, if any
    run_id: Optional[int]


def current_caller() -> Caller:
    from tools import kanban_tools as kt

    if kt._delegation_ctx("is_delegated_child_process_context", False):
        raise ItemRefused("delegate_task child agents do not own Kanban cards; return findings to the "
                          "parent agent, which parks the item")
    env_task = kt._default_task_id(None)
    run_id = kt._worker_run_id(env_task) if env_task else None
    return Caller(profile=kt._persisted_identity(), env_task=env_task, run_id=run_id)


def _guard_text(text: str, where: str) -> None:
    """No decision markers and nothing secret-looking: the stored bytes are hashed, so a later
    redaction pass must have nothing to change."""
    refuse_decision_text(text, where)
    masked = _PHONE_RE.sub("+0", text)
    if redact_sensitive_text(masked, force=True) != masked:
        raise SchemaError(f"{where}: contains a secret-looking string (token, key or password); "
                          "remove it, items are shown to Jack and copied into the board archive")


def _resolve_card(caller: Caller, card: Any) -> str:
    explicit = str(card).strip() if card not in (None, "") else None
    if caller.env_task:
        if explicit and explicit != caller.env_task:
            raise ItemRefused(f"this worker is scoped to {caller.env_task}; refusing to park {explicit}")
        return caller.env_task
    if not explicit:
        raise ItemRefused("card is required: this session has no dispatcher-assigned task. Pass the id "
                          "of a card you own (you are its assignee or created it)")
    return explicit


def _require_owner(caller: Caller, task, *, actor: Optional[str] = None) -> None:
    """Add: the caller's own dispatcher card, or one it is assigned or created. Update/withdraw
    (``actor`` given): the profile that parked the item or the card's current assignee (§4.2)."""
    if caller.env_task:
        if task.id != caller.env_task:
            raise ItemRefused(f"this worker is scoped to {caller.env_task}; refusing to change {task.id}")
        if caller.run_id is None:
            raise ItemRefused("this worker cannot resolve its HERMES_KANBAN_RUN_ID, so it cannot prove it "
                              "owns the card's run; re-run through the dispatcher")
    owners = {task.assignee, actor} if actor is not None else {task.assignee, task.created_by}
    if caller.profile not in owners and not (actor is None and caller.env_task == task.id):
        raise ItemRefused(f"{caller.profile} does not own {task.id} (assignee {task.assignee!r}"
                          + (f", parked by {actor!r})" if actor is not None else f", creator {task.created_by!r})"))


def _board_slug(kb, board: Optional[str]) -> str:
    return board or kb.get_current_board()


def _prepare(env: dict) -> tuple[str, str]:
    """The comment and park reason, refused unless the reason reads back as this exact item: the
    add-time gate is the read-time trust check (§3.1), so nothing is written that would load
    untrusted (a bound id with a space is cut short by the space-delimited reason)."""
    body, reason = comment_body(env), park_reason(env)
    _guard_text(body, "item")
    _guard_text(reason, "park reason")
    problems = binding_problems(env, reason)
    if reason_item_hash(reason) != comment_hash16(body):
        problems.append("its item= does not bind the item comment")
    if problems:
        raise SchemaError("the park reason cannot carry this item (" + "; ".join(problems)
                          + "); bound ids and link URLs must not contain whitespace")
    return body, reason


def _refuse_duplicate(kb, conn, card: str, body: str) -> None:
    if any(c.body == body for c in kb.list_comments(conn, card)):
        raise ItemRefused("an identical item comment already exists on this card, so its item= hash "
                          "would bind two comments; change a field (why, ask, payload) first")


def _park(kb, conn, task, caller: Caller, reason: str, current: Optional[ItemView]) -> Optional[int]:
    """The park write; returns the new park event id, or None when the board refused it."""
    from hermes_cli.kanban_db_repark import latest_park_event, repark_blocked_task

    if task.status == "blocked" and current is not None:
        return repark_blocked_task(conn, task.id, expected_park_event_id=current.event_id,
                                   reason=reason, actor=caller.profile)
    run_id = caller.run_id if caller.env_task == task.id else None
    if not kb.block_task(conn, task.id, reason=reason, kind="needs_input", expected_run_id=run_id,
                         actor=caller.profile):
        return None
    return int(latest_park_event(conn, task.id)["id"])


def _write(kb, conn, board: str, task, caller: Caller, env: dict,
           current: Optional[ItemView] = None) -> dict:
    body, reason = _prepare(env)
    _refuse_duplicate(kb, conn, task.id, body)
    kb.add_comment(conn, task.id, author=caller.profile, body=body)
    event_id = _park(kb, conn, task, caller, reason, current)
    if event_id is None:
        raise ItemRefused(f"could not park {task.id} (its status changed); the item comment was written "
                          "but no park binds it, so it is never shown. Nothing else changed")
    spec = spec_for(env["kind"])
    landed = kb.get_task(conn, task.id)
    return {"ok": True, "id": item_id(board, task.id, event_id), "binding_hash": spec.binding_hash(env),
            "render_as": spec.kind, "item": reason.rsplit("item=", 1)[1], "requested_by": caller.profile,
            "card": task.id, "card_status": landed.status if landed else None, "park_reason": reason}


def _envelope_args(args: dict) -> dict:
    return {key: args.get(key) for key in ENVELOPE_ARGS}


def add_item(args: dict, *, sources: Optional[LiveSources] = None) -> dict:
    from tools.kanban_tools import _board

    caller = current_caller()
    card = _resolve_card(caller, args.get("card"))
    env = build_envelope(args.get("kind"), payload=args.get("payload"), sources=sources,
                         **_envelope_args(args))
    with _board(args.get("board")) as (kb, conn):
        task = kb.get_task(conn, card)
        if task is None:
            raise ItemRefused(f"card {card} not found")
        _require_owner(caller, task)
        if task.status not in ADDABLE_STATUSES or (task.status == "running" and caller.env_task != card):
            raise ItemRefused(f"card {card} is {task.status}: items are added to a card you are running, "
                              "or a ready card you own; a parked card is changed with action_item_update")
        return _write(kb, conn, _board_slug(kb, args.get("board")), task, caller, env)


def _open_item(kb, conn, raw_id: Any, caller: Caller) -> tuple[ItemView, Any]:
    """A trusted action-item park the caller owns (§4.2). Legacy parks and the loop breaker's
    ``block_loop_detected`` park are not action items: they carry no parking profile, and lifting
    them is ``kanban_unblock``'s (orchestrator-only) job, so update and withdraw refuse them."""
    board, card, event_id = parse_item_id(raw_id)
    view = load_item(kb, conn, board, card, event_id)
    if view.event_kind != "blocked":
        raise ItemRefused(f"{view.id} is the loop breaker's {view.event_kind} park, not an action item; "
                          "only an orchestrator lifts it")
    if view.envelope is None or not view.trusted or not view.actor:
        raise ItemRefused(f"{view.id} is not a trusted action item ({'; '.join(view.problems) or 'no actor'}); "
                          "only an item parked with action_item_add can be updated or withdrawn")
    task = kb.get_task(conn, card)
    _require_owner(caller, task, actor=view.actor)
    from hermes_cli.kanban_db_repark import latest_park_event

    current = latest_park_event(conn, card)
    if current is None or int(current["id"]) != event_id:
        raise ItemRefused(f"{view.id} is no longer the card's current park"
                          + (f" (now {item_id(board, card, current['id'])})" if current is not None else ""))
    return view, task


def update_item(args: dict, *, sources: Optional[LiveSources] = None) -> dict:
    from tools.kanban_tools import _board

    caller = current_caller()
    board = parse_item_id(args.get("id"))[0]
    with _board(board) as (kb, conn):
        view, task = _open_item(kb, conn, args.get("id"), caller)
        old = view.envelope
        spec = spec_for(old["kind"])
        payload = {k: v for k, v in old["payload"].items() if k in spec.agent_fields}
        if spec.kind in ("send_email", "send_message"):
            payload.pop("content_hash", None)  # recomputed from the changed draft fields
        payload.update(args.get("payload") or {})
        fields = {key: (args[key] if key in args else old.get(key)) for key in ENVELOPE_ARGS}
        env = build_envelope(old["kind"], payload=payload, sources=sources, **fields)
        if canonical_json(env) == canonical_json(old):
            raise ItemRefused("nothing changed: the new item is identical to the current one")
        if task.status not in ("blocked", *ADDABLE_STATUSES) or \
                (task.status == "running" and caller.env_task != task.id):
            raise ItemRefused(f"card {task.id} is {task.status}; it cannot be re-parked now")
        result = _write(kb, conn, board, task, caller, env, current=view)
        return {**result, "replaces": view.id}


def withdraw_item(args: dict) -> dict:
    from tools.kanban_tools import _board

    caller = current_caller()
    reason = args.get("reason")
    if not isinstance(reason, str):
        raise SchemaError("reason: must be a string")
    check_text(reason, "line", "reason")
    if len(reason) > 500:
        raise SchemaError("reason: max 500 chars")
    board = parse_item_id(args.get("id"))[0]
    with _board(board) as (kb, conn):
        view, task = _open_item(kb, conn, args.get("id"), caller)
        if task.status != "blocked":
            raise ItemRefused(f"{view.id} is not pending: card is {task.status}, so there is nothing to withdraw")
        body = f"{WITHDRAWN_PREFIX}:{view.id}] {reason}"
        _guard_text(body, "reason")
        kb.add_comment(conn, task.id, author=caller.profile, body=body)
        unblocked = kb.unblock_task(conn, task.id)
        landed = kb.get_task(conn, task.id)
        return {"ok": True, "id": view.id, "status": "withdrawn", "unblocked": unblocked,
                "card": task.id, "card_status": landed.status if landed else None}


def get_decision(args: dict) -> dict:
    """Read-only, for any profile: Jack's answer to an item and whether it verifies (§4.3)."""
    from hermes_cli.kanban_db_repark import PARK_EVENTS
    from tools.action_item_decision import decision_for
    from tools.kanban_tools import _board

    board, card, event_id = parse_item_id(args.get("id"))
    with _board(board) as (kb, conn):
        view = load_item(kb, conn, board, card, event_id)
        later = [e.created_at for e in kb.list_events(conn, card)
                 if e.kind in PARK_EVENTS and e.id > event_id]
        task = kb.get_task(conn, card)
        return decision_for(view, kb.list_comments(conn, card), later[0] if later else None,
                            assignee=task.assignee if task else None)
