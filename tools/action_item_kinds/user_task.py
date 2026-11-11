"""``user_task`` v1: something only Jack can do (sign in, approve a consent screen) (design §3.2).

Resolved by Jack's ``done``; the card owner then verifies it really happened.
"""

from __future__ import annotations

from typing import Optional

from tools.action_item_kinds import legacy
from tools.action_item_kinds.core import prefixed_hash
from tools.action_item_kinds.spec import KindSpec, load_versions, no_external_live_check, require_hash

KIND = "user_task"
AGENT_FIELDS = frozenset({"steps", "where"})


def steps_hash(payload: dict) -> str:
    return prefixed_hash({"steps": payload.get("steps"), "where": payload.get("where")})


def _enrich(agent: dict, sources) -> dict:
    return {**agent, "steps_hash": steps_hash(agent)}


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
