"""Carry Jack's verified approval across a same-artifact re-park on one card (Jack, Nov 12).

A worker that re-parks the exact draft or PR head Jack already approved must not ask him twice. So a
park with no decision of its own takes over the approval of the newest earlier park on the SAME card
when, walking back park by park:

* every park in between is a trusted item of the same kind with the same ``binding_hash`` (the kind's
  binding fields: account + thread + draft + content hash for ``send_email``, channel + account +
  conversation + draft ref + content hash for ``send_message``, repo + number + head + base + head
  branch + method + admin + promotion for ``pr_merge``), and none of them was decided or withdrawn;
* the earlier park carries a verified web approval (``action_item_decision.verify``);
* no ``[action-done`` / ``[action-failed`` / ``[jack-rejected`` comment follows that approval before
  the new park: one approval is one action, and a rejection or an attempt ends it.

Only the machine gates carry (``CARRY_KINDS``): their artifact is fully bound and their action is
marked once by ``[action-done:<gate>]``. A ``design_decision`` / ``user_task`` / ``other`` approval
hands the card back and is read once by the owner, so a re-asked one is a new question for Jack.

The source keeps its decision but reads ``status: carried`` with ``carried_to``; the new park reads
``verified: true`` with ``carried_from`` (the park Jack decided). Nothing is written here: the carry
is recomputed from the card on every read, so it can never outlive the card's own comments. Read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from tools.action_item_decision import decide
from tools.action_item_view import ItemView, load_item

CARRY_KINDS = frozenset({"pr_merge", "send_email", "send_message"})
# The kinds whose owner acts on the approval: a carried one hands the card straight back. A merge
# stays parked: only the merge executor merges, and it verifies its own approval.
HAND_BACK_KINDS = frozenset({"send_email", "send_message"})
_SPENT = ("[action-done", "[action-failed", "[jack-rejected")
_COPIED = ("verdict", "option", "answers", "note", "decided_by", "via", "at", "audit_id")


def artifact(view: ItemView) -> Optional[tuple[str, str]]:
    """``(kind, binding_hash)`` of a trusted machine-gate item, else None (it never carries)."""
    if not view.trusted or view.envelope is None:
        return None
    kind = view.classification.get("render_as")
    binding = view.classification.get("binding_hash")
    return (kind, binding) if kind in CARRY_KINDS and binding else None


@dataclass
class _Held:
    source: ItemView  # the park Jack approved
    comment: Any  # his decision comment
    holder: dict  # the newest result holding the approval (the source's, then each carry's)


def _spent(held: _Held, comments: list, park_at: int) -> bool:
    """An action, an attempt or a rejection after the approval, up to the new park (same second
    included: an ambiguous order never carries)."""
    return any(c.id > held.comment.id and c.created_at <= park_at and c.body.lstrip().startswith(_SPENT)
               for c in comments)


def _outcome(comments: list, view: ItemView, until: Optional[int]) -> Optional[str]:
    for c in comments:
        if c.created_at > view.event_at and (until is None or c.created_at < until):
            if c.body.startswith("[action-done"):
                return "done"
            if c.body.startswith("[action-failed"):
                return "failed"
    return None


def _carry(held: _Held, view: ItemView, result: dict, comments: list, until: Optional[int]) -> None:
    result.update({key: held.holder[key] for key in _COPIED}, status="decided", verified=True, problems=[],
                  carried_from=held.source.id)
    outcome = _outcome(comments, view, until)
    if outcome:
        result["status"] = result["outcome"] = outcome
    held.holder.update(status="carried", outcome="carried", carried_to=view.id)
    held.holder = result


def card_decisions(kb, conn, board: str, card: str, assignee: Optional[str]) -> dict[int, dict]:
    """Every park's ``action_item_get_decision`` result on ``card``, carries applied, by event id."""
    from hermes_cli.kanban_db_repark import PARK_EVENTS

    parks = sorted((e for e in kb.list_events(conn, card) if e.kind in PARK_EVENTS), key=lambda e: e.id)
    comments = kb.list_comments(conn, card)
    out: dict[int, dict] = {}
    held: Optional[_Held] = None
    for n, event in enumerate(parks):
        until = parks[n + 1].created_at if n + 1 < len(parks) else None
        view = load_item(kb, conn, board, card, event.id)
        result, comment = decide(view, comments, until, assignee)
        out[event.id] = result
        undecided = result["verdict"] is None and result["status"] != "withdrawn"
        if undecided and held is not None and view.event_kind == "blocked" \
                and artifact(view) == artifact(held.source) and not _spent(held, comments, view.event_at):
            _carry(held, view, result, comments, until)
            continue
        verified_approval = (comment is not None and result["status"] == "decided"
                             and result["verdict"] == "approve" and result["verified"])
        held = _Held(view, comment, result) if verified_approval and artifact(view) else None
    return out
