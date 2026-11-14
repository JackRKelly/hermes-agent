"""Is a block reason a park for Jack? One answer for the block router and the notifier.

The test mirrors the one Action Items uses to put a block in Jack's queue (assistant
``scripts/kanban_gates.py`` ``block_owner``), and the FIRST marker decides: an approval gate that
leads its owner tag and any ``[needs-attention]`` marker, or a first owner tag that is Jack's. A
reason with no owner tag that binds a typed action item (``item=<hash16>``) is Jack's too. A
``[captain:..]`` or ``[hold:..]`` block quoting a gate, a Jack tag or an item binding is not.
"""

from __future__ import annotations

import json
import re
from typing import Optional

_APPROVAL_GATE_RE = re.compile(r"\[awaiting-approval:[a-z-]+\]")
_ATTENTION_RE = re.compile(r"\[needs-attention\]")
_OWNER_TAG_RE = re.compile(r"\[(?P<owner>needs-jack|decision|captain|hold)(?::[\w-]+)?\]", re.IGNORECASE)
_JACK_OWNER_TAGS = frozenset({"needs-jack", "decision"})


def is_jack_park_reason(reason: Optional[str]) -> bool:
    """True when a block with this reason waits on Jack (an Action Item or a tagged decision)."""
    reason = reason or ""
    owner = _OWNER_TAG_RE.search(reason)
    attention = _ATTENTION_RE.search(reason)
    gate = _APPROVAL_GATE_RE.search(reason)
    if gate:
        if all(gate.start() < m.start() for m in (owner, attention) if m):
            return True
        if attention and not (owner and owner.start() < attention.start()):
            return False
    if owner:
        return owner["owner"].lower() in _JACK_OWNER_TAGS
    if attention:
        return False
    # Late: the package ``__init__`` loads every kind's schema, which the block path never needs.
    from tools.action_item_kinds.core import reason_item_hash

    return reason_item_hash(reason) is not None


# The three markers Captain's runbook treats as one class: the run stopped at a safe point (turn
# budget, phase boundary) and Captain unblocks it at once to re-run from the checkpoint.
_CONTINUE_RE = re.compile(r"\[captain:(?:continue|continuation|turn-budget)\]", re.IGNORECASE)

# A continue handoff re-parks by design, but Captain (an agent) unblocks it with no human step, so
# a card that hands off this many times in a row (see ``continue_streak``) is looping: the handoff
# that reaches this count takes the normal triage route.
CONTINUE_RECURRENCE_LIMIT = 5

# Every event that ends a run of continue handoffs when it is not itself a continue block.
_STREAK_EVENT_KINDS = ("blocked", "block_loop_detected", "dependency_wait", "completed")


def continue_streak(conn, task_id: str) -> int:
    """Continue handoffs in a row on this card, newest first, before the block being written now.

    Counted from the event log, not the per-kind ``block_recurrences`` counter, which every
    same-kind block raises (Jack parks included) and only completion clears. The run ends at the
    last completion or the last block of any other cause: a Jack park or a triage bounce is a human
    step, and another cause is the normal breaker's to count. Call inside the block write txn.
    """
    rows = conn.execute(
        f"SELECT kind, payload FROM task_events WHERE task_id = ? AND kind IN "
        f"({','.join('?' * len(_STREAK_EVENT_KINDS))}) ORDER BY id DESC",
        (task_id, *_STREAK_EVENT_KINDS),
    )
    streak = 0
    for kind, payload in rows:
        reason = (json.loads(payload or "{}") or {}).get("reason")
        if kind != "blocked" or not is_captain_continue_reason(reason):
            break
        streak += 1
    return streak


def is_captain_continue_reason(reason: Optional[str]) -> bool:
    """True when the FIRST marker is a continue handoff (``[captain:continue]``, ``continuation``,
    ``turn-budget``): a clean handoff Captain re-runs. Other ``[captain:<topic>]`` reasons are
    problems Captain must look at and keep the normal triage escalation.
    """
    reason = reason or ""
    first = min(
        (m for rx in (_APPROVAL_GATE_RE, _ATTENTION_RE, _OWNER_TAG_RE) if (m := rx.search(reason))),
        key=lambda m: m.start(), default=None,
    )
    return first is not None and bool(_CONTINUE_RE.fullmatch(first.group(0)))
