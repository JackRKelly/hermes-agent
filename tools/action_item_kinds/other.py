"""``other`` v1: the free-form card, and the read-time fallback for any item that fails its kind.

``body`` is the only field of any kind that may use the §5.4 markdown subset.
"""

from __future__ import annotations

from typing import Optional

from tools.action_item_kinds import legacy
from tools.action_item_kinds.core import HTML_TAG_RE, sha256_hex
from tools.action_item_kinds.spec import KindSpec, load_versions, no_external_live_check, require_hash

KIND = "other"
AGENT_FIELDS = frozenset({"title", "body", "links"})


def body_hash(payload: dict) -> str:
    return "sha256:" + sha256_hex(str(payload.get("body", "")))


def _enrich(agent: dict, sources) -> dict:
    return {"links": [], **agent, "body_hash": body_hash(agent)}


def _check(env: dict) -> None:
    require_hash(env["payload"], "body_hash", body_hash(env["payload"]))


def _legacy(reason: str) -> Optional[dict]:
    """Any park becomes an ``other`` built from its reason text (approve = record and hand back)."""
    text = HTML_TAG_RE.sub("", (reason or "").strip())
    if not text:
        return None
    gate = legacy.parse_gate(text)
    first = text.split("\n", 1)[0]
    title = (f"Approve {gate[0]}: {gate[1]}" if gate else first)[:140]
    return {"title": title, "body": text[:4000], "links": []}


SPEC = KindSpec(
    kind=KIND, park_tag="needs-jack", gate="other",
    schema_versions=load_versions(KIND, 1), agent_fields=AGENT_FIELDS,
    binding_fields=("title", "body_hash", "links"),
    allowed_actions=("approve", "reject", "snooze", "later"),
    executor=None, enrich=_enrich, live_check=no_external_live_check("body_hash", body_hash),
    legacy_parse=_legacy, check=_check,
)
