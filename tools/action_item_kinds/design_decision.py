"""``design_decision`` v1: Jack picks one of 2-6 options (design §3.2).

The tool computes ``option_set_hash`` over the question and the options, so a re-worded option is a
new item. Exactly one option is the recommendation; ``other`` is reserved for Jack's free answer.
"""

from __future__ import annotations

from typing import Optional

from tools.action_item_kinds.core import SchemaError, prefixed_hash
from tools.action_item_kinds.spec import KindSpec, load_versions, no_external_live_check, require_hash

KIND = "design_decision"
AGENT_FIELDS = frozenset({"question", "options", "rec_reason"})


def option_set_hash(payload: dict) -> str:
    return prefixed_hash({"question": payload.get("question"), "options": payload.get("options")})


def _enrich(agent: dict, sources) -> dict:
    return {**agent, "option_set_hash": option_set_hash(agent)}


def _check(env: dict) -> None:
    p = env["payload"]
    keys = [o["key"] for o in p["options"]]
    if len(set(keys)) != len(keys):
        raise SchemaError("payload.options: option keys must be unique")
    if "other" in keys:
        raise SchemaError('payload.options: "other" is reserved for Jack\'s own answer')
    if sum(1 for o in p["options"] if o["recommended"]) != 1:
        raise SchemaError("payload.options: mark exactly one option recommended")
    require_hash(p, "option_set_hash", option_set_hash(p))


def _legacy(reason: str) -> Optional[dict]:
    return None  # a free-text decision park carries no option set; it shows as `other`


SPEC = KindSpec(
    kind=KIND, park_tag="needs-jack", gate="decision",
    schema_versions=load_versions(KIND, 1), agent_fields=AGENT_FIELDS,
    binding_fields=("question", "option_set_hash"),
    allowed_actions=("pick_option", "other", "reject", "snooze", "later"),
    executor=None, enrich=_enrich,
    live_check=no_external_live_check("option_set_hash", option_set_hash),
    legacy_parse=_legacy, check=_check,
)
