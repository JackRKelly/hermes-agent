"""The backend kinds registry: ``KINDS``, ``KINDS_VERSION`` and the envelope rules (design §3.2).

Bump ``KINDS_VERSION`` whenever a kind or a schema version is added or changed: the dashboard plugin
refuses to install or load against a registry version it was not built for.

Version 2: every new envelope carries a human ``title`` and ``summary`` (envelope schema v2, which
still reads a v1 envelope without them), ``design_decision`` v2 asks 1-5 questions that Jack
answers (never approves), and a free-text ``[needs-jack:`` park reads as a ``design_decision``.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

from tools.action_item_kinds import (
    design_decision, other, pr_merge, readability, send_email, send_message, user_task,
)
from tools.action_item_kinds.core import SchemaError, item_hash
from tools.action_item_kinds.hostcfg import link_hosts
from tools.action_item_kinds.schema_lite import load_schema, validate
from tools.action_item_kinds.spec import FALLBACK_ACTIONS, KINDS_DIR, KindSpec, LiveSources

KINDS_VERSION = 2
# The envelope fields an agent passes as top-level tool parameters (never inside ``payload``).
ENVELOPE_PARAMS = ("title", "summary", "ask", "why", "risk", "deadline", "links", "then", "batchable")
KINDS: Mapping[str, KindSpec] = MappingProxyType({spec.kind: spec for spec in (
    pr_merge.SPEC, send_email.SPEC, send_message.SPEC, design_decision.SPEC, user_task.SPEC, other.SPEC,
)})
ENVELOPE_VERSION = 2
ENVELOPE_SCHEMA = load_schema(KINDS_DIR / f"envelope.v{ENVELOPE_VERSION}.schema.json")
# Legacy free-text parks, most specific first; ``design_decision`` takes every remaining
# ``[needs-jack:`` park and ``other`` takes anything left.
_LEGACY_ORDER = ("pr_merge", "send_email", "send_message", "user_task", "design_decision", "other")
_FIELD_RE = re.compile(r"^((?:envelope|payload)[\w.\[\]]*)\s*:")


def spec_for(kind: Any) -> KindSpec:
    spec = KINDS.get(kind) if isinstance(kind, str) else None
    if spec is None:
        raise SchemaError(f"kind: must be one of {sorted(KINDS)} ({kind!r})")
    return spec


def build_envelope(kind: str, *, payload: Any, title: Any, summary: Any, ask: Any, why: Any, risk: Any,
                   deadline: Any = None, links: Any = None, then: Any = None, batchable: Any = None,
                   sources: Optional[LiveSources] = None, hosts: Optional[Iterable[str]] = None,
                   card: Optional[str] = None) -> dict:
    """A validated envelope from an agent's call: tool fields are filled by the kind's ``enrich``
    (reading the live source), never taken from the agent. The add-time readability rules
    (``readability.py``) apply here only, so a stored item still reads. ``card`` is the card the item
    parks on: its own workspace is the one place a text may name a file by path; with no card, every
    local path is refused. Raises ``SchemaError``."""
    spec = spec_for(kind)
    if not isinstance(payload, dict):
        raise SchemaError("payload: must be an object")
    spec.precheck(payload)
    tool_fields = sorted(set(payload) - spec.agent_fields)
    if tool_fields:
        misplaced = [key for key in tool_fields if key in ENVELOPE_PARAMS]
        hint = (f"; {misplaced} are top-level parameters of the call beside kind and payload, never payload "
                "fields" if misplaced else "")
        raise SchemaError(f"payload: {tool_fields} are filled by the tool from the live source; "
                          f"pass only {sorted(spec.agent_fields)}{hint}")
    readability.check_new_envelope(title=title, summary=summary, risk=risk)
    readability.check_item_paths(spec.kind, payload, ask=ask, why=why, risk=risk, card=card)
    if sources is None:
        from tools.action_item_kinds.github import GhSources
        sources = GhSources()
    full = spec.enrich(dict(payload), sources)
    env: dict = {"kind": spec.kind, "schema": spec.current_schema, "title": title, "summary": summary,
                 "ask": ask, "why": why, "risk": risk, "deadline": deadline,
                 "links": spec.default_links(full) if links is None else links,
                 "then": spec.default_then if then is None else then, "payload": full}
    if spec.source_key is not None:
        env["source_key"] = spec.source_key(full)
    if batchable is not None:
        env["batchable"] = batchable
    validate_envelope(env, hosts)
    return env


def validate_envelope(env: Any, hosts: Optional[Iterable[str]] = None) -> KindSpec:
    """The add-time and read-time gate; returns the kind's spec or raises ``SchemaError``."""
    allowed_hosts = frozenset(hosts) if hosts is not None else link_hosts()
    validate(env, ENVELOPE_SCHEMA, link_hosts=allowed_hosts, where="envelope")
    spec = spec_for(env["kind"])
    schema = spec.schema_versions.get(env["schema"])
    if schema is None:
        raise SchemaError(f"schema: {spec.kind} has versions {sorted(spec.schema_versions)} ({env['schema']!r})")
    if env["then"] not in spec.then_values:
        raise SchemaError(f"then: must be one of {list(spec.then_values)} for {spec.kind}")
    if (spec.source_key is None) == ("source_key" in env):
        raise SchemaError(f"source_key: {'required' if spec.source_key else 'not used'} for {spec.kind}")
    if "batchable" in env and not spec.batchable:
        raise SchemaError(f"batchable: {spec.kind} items are never batch-eligible")
    validate(env["payload"], schema, link_hosts=allowed_hosts, where="payload")
    spec.check(env)
    return spec


def park_reason(env: dict) -> str:
    """The block reason binding ``env``: today's gate format plus ``then=`` and ``item=<hash16>``."""
    spec = spec_for(env["kind"])
    artifact, params = spec.park_params(env)
    words = [f"[{spec.park_tag}:{spec.gate}]", artifact, *(f"{k}={v}" for k, v in params),
             f"then={env['then']}", f"item={item_hash(env)}"]
    return " ".join(words)


def classify(env: Any, hosts: Optional[Iterable[str]] = None) -> dict:
    """Read-time projection of a stored envelope: never raises, never hides the item. An unknown
    kind, an unknown schema version or an invalid payload renders as ``other`` with approve off."""
    try:
        spec = validate_envelope(env, hosts)
    except SchemaError as exc:
        kind = env.get("kind") if isinstance(env, dict) else None
        known = isinstance(kind, str) and kind in KINDS
        version_ok = known and isinstance(env, dict) and env.get("schema") in KINDS[kind].schema_versions
        reason = "schema" if version_ok else "schema_version" if known else "unknown_kind"
        field = _FIELD_RE.match(str(exc))
        return {"render_as": "other", "allowed_actions": FALLBACK_ACTIONS, "binding_hash": None,
                "fallback": {"reason": reason, "failed": [field.group(1)] if field else [], "error": str(exc)}}
    return {"render_as": spec.kind, "allowed_actions": spec.actions_for(env["payload"], env["schema"]),
            "binding_hash": spec.binding_hash(env), "fallback": None}


def legacy_payload(reason: str) -> Optional[tuple[str, dict]]:
    """``(kind, payload)`` typed from a free-text park, or None for an empty reason. A
    ``design_decision`` payload is ``design_decision.legacy_questions``' read-only projection."""
    for kind in _LEGACY_ORDER:
        parsed = KINDS[kind].legacy_parse(reason)
        if parsed is not None:
            return kind, parsed
    return None
