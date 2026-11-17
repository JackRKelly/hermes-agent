"""``user_task`` v1: something only Jack can do (sign in, approve a consent screen) (design §3.2).

Resolved by Jack's ``done``; the card owner then verifies it really happened.
"""

from __future__ import annotations

import re
from typing import Optional

from tools.action_item_kinds import legacy
from tools.action_item_kinds.core import prefixed_hash
from tools.action_item_kinds.spec import KindSpec, load_versions, no_external_live_check, require_hash

KIND = "user_task"
AGENT_FIELDS = frozenset({"steps", "where"})


# A step's own list marker ("1.", "2)", "- "): the app shows steps in a numbered list, so a stored marker
# reads "1. 1. Sign in ..." (Jack, Nov 12). Only a marker followed by a space: "2FA" and "1.5 GB" stay.
STEP_MARKER_RE = re.compile(r"^\s*(?:\d{1,3}[.)]|[-*\u2022])\s+")


def normalize_steps(steps):
    """Each step without its own list marker; anything that is not a list of strings is left to the schema."""
    if not isinstance(steps, list):
        return steps
    return [STEP_MARKER_RE.sub("", s, count=1) if isinstance(s, str) else s for s in steps]


def steps_hash(payload: dict) -> str:
    return prefixed_hash({"steps": payload.get("steps"), "where": payload.get("where")})


def _enrich(agent: dict, sources) -> dict:
    full = {**agent, "steps": normalize_steps(agent.get("steps"))} if "steps" in agent else dict(agent)
    return {**full, "steps_hash": steps_hash(full)}


def _check(env: dict) -> None:
    require_hash(env["payload"], "steps_hash", steps_hash(env["payload"]))


def _legacy(reason: str) -> Optional[dict]:
    """``[needs-jack:sign-in ...] <ask>`` (the one legacy your-task form that is well typed)."""
    parsed = legacy.parse_needs_jack(reason)
    if not parsed or not parsed[0].startswith("sign-in") or not parsed[1]:
        return None
    return {"steps": [parsed[1][:300]], "where": ""}


SPEC = KindSpec(
    kind=KIND, park_tag="needs-jack", gate="user-task",
    schema_versions=load_versions(KIND, 1), agent_fields=AGENT_FIELDS,
    binding_fields=("steps_hash",),
    allowed_actions=("done", "reject", "snooze", "later"),
    executor=None, enrich=_enrich, live_check=no_external_live_check("steps_hash", steps_hash),
    legacy_parse=_legacy, check=_check,
)
