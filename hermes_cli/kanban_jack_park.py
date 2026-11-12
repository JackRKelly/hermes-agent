"""Is a block reason a park for Jack? One answer for the block router and the notifier.

The test mirrors the one Action Items uses to put a block in Jack's queue (assistant
``scripts/kanban_gates.py`` ``block_owner``), and the FIRST marker decides: an approval gate that
leads its owner tag and any ``[needs-attention]`` marker, or a first owner tag that is Jack's. A
reason with no owner tag that binds a typed action item (``item=<hash16>``) is Jack's too. A
``[captain:..]`` or ``[hold:..]`` block quoting a gate, a Jack tag or an item binding is not.
"""

from __future__ import annotations

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
