"""Resolve an action item id to its park, its item comment and the §3.1 trust checks.

Shared by the ``action_item_*`` tools (update / withdraw / get_decision) and importable by the
dashboard plugin's projection, so both read a park the same way. Read-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from tools.action_item_kinds import legacy
from tools.action_item_kinds.core import comment_hash16, parse_comment, reason_item_hash
from tools.action_item_kinds.registry import KINDS, classify

ITEM_ID_RE = re.compile(r"^(?P<board>[A-Za-z0-9][A-Za-z0-9_-]{0,63}):(?P<card>[A-Za-z0-9_-]{1,64})@(?P<event>[0-9]{1,18})$")


class ItemRefused(ValueError):
    """An action item call that cannot proceed; nothing was written."""


def parse_item_id(item_id: Any) -> tuple[str, str, int]:
    match = ITEM_ID_RE.match(str(item_id or "").strip())
    if not match:
        raise ItemRefused(f"id must look like <board>:<card>@<block_event_id> ({item_id!r})")
    return match["board"], match["card"], int(match["event"])


def item_id(board: str, card: str, event_id: int) -> str:
    return f"{board}:{card}@{int(event_id)}"


@dataclass
class ItemView:
    id: str
    board: str
    card: str
    event_id: int
    event_at: int
    reason: str
    actor: Optional[str]
    event_kind: str = "blocked"  # or the loop breaker's ``block_loop_detected``
    comment: Any = None  # the bound kanban Comment, when exactly one matches
    envelope: Optional[dict] = None
    classification: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def trusted(self) -> bool:
        return not self.problems


def binding_problems(env: dict, reason: str) -> list[str]:
    """§3.1 rule 3: the gate parameters in the park reason agree with the payload's bound fields."""
    spec = KINDS[env["kind"]]
    parsed = legacy.parse_gate(reason) if spec.park_tag == "awaiting-approval" else legacy.parse_needs_jack(reason)
    if parsed is None or parsed[0] != spec.gate:
        return [f"park tag is not [{spec.park_tag}:{spec.gate}]"]
    _, expected = spec.park_params(env)
    return [f"park {key}= does not match the payload" for key, value in expected
            if parsed[2].get(key) != value]


def load_item(kb, conn, board: str, card: str, event_id: int) -> ItemView:
    """The park event ``event_id`` on ``card`` and its item comment, with every trust problem."""
    from hermes_cli.kanban_db_repark import PARK_EVENTS

    task = kb.get_task(conn, card)
    if task is None:
        raise ItemRefused(f"card {card} not found on board {board}")
    event = next((e for e in kb.list_events(conn, card) if e.id == event_id), None)
    if event is None or event.kind not in PARK_EVENTS:
        raise ItemRefused(f"{item_id(board, card, event_id)} is not a park on {card}")
    payload = event.payload or {}
    view = ItemView(id=item_id(board, card, event_id), board=board, card=card, event_id=event_id,
                    event_at=event.created_at, reason=str(payload.get("reason") or ""),
                    actor=payload.get("actor"), event_kind=event.kind)
    wanted = reason_item_hash(view.reason)
    if wanted is None:
        view.problems.append("legacy park: the reason binds no item comment")
        return view
    matches = [c for c in kb.list_comments(conn, card) if comment_hash16(c.body) == wanted]
    if len(matches) != 1:
        view.problems.append(f"item={wanted} matches {len(matches)} item comments, not exactly one")
        return view
    view.comment, view.envelope = matches[0], parse_comment(matches[0].body)
    if not view.actor or view.comment.author != view.actor:
        view.problems.append("the item comment's author is not the profile that parked it")
    view.classification = classify(view.envelope)
    if view.classification["fallback"]:
        view.problems.append(f"payload fails its schema: {view.classification['fallback']['error']}")
    else:
        view.problems.extend(binding_problems(view.envelope, view.reason))
    return view
