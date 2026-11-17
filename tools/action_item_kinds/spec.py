"""``KindSpec``: everything the backend knows about one action item kind (design §3.2).

A kind module builds one ``KindSpec`` and ``registry.py`` collects them. The frontend renderers and
the executors live in the assistant repo; ``executor`` only names one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from tools.action_item_kinds.core import SchemaError, prefixed_hash
from tools.action_item_kinds.schema_lite import load_schema

KINDS_DIR = Path(__file__).resolve().parent
FALLBACK_ACTIONS = ("reject", "snooze", "later")


class SourceUnavailable(RuntimeError):
    """A live source (GitHub, Gmail, a message thread) could not be read: callers fail closed."""


class LiveSources:
    """Readers for the live artifacts. hermes-agent ships the GitHub reader (``gh``); a host that
    can read Gmail drafts or message threads (the dashboard plugin, an executor) subclasses this.
    Every method raises ``SourceUnavailable`` when it cannot answer."""

    def github_pr(self, repo: str, number: int) -> dict:
        raise SourceUnavailable("no GitHub reader configured")

    def github_merge_method(self, repo: str) -> str:
        raise SourceUnavailable("no GitHub reader configured")

    def gmail_draft(self, account: str, draft_id: str) -> Optional[dict]:
        """``{to, cc, bcc, subject, body, attachments}`` of the draft, or None when it is gone."""
        raise SourceUnavailable("no Gmail draft reader in hermes-agent; the send handler re-checks")

    def conversation(self, channel: str, account: str, conversation_id: str) -> dict:
        """``{last_inbound_at}`` of the thread."""
        raise SourceUnavailable("no message-thread reader in hermes-agent; the card owner re-checks")

    def slack_conversation(self, channel_id: str, thread_ts: str, oldest: float) -> dict:
        """``{"thread": [...], "channel": [...]}``: the raw Slack messages (``ts``, ``user``, ``text``,
        ...) of ``conversations.replies`` for the thread and ``conversations.history`` of the channel,
        each from ``oldest`` (epoch seconds, inclusive) on."""
        raise SourceUnavailable("no Slack reader configured")


def load_versions(kind: str, *versions: int) -> dict[int, dict]:
    return {v: load_schema(KINDS_DIR / f"{kind}.v{v}.schema.json") for v in versions}


def live_result(drift: list[str], **facts: Any) -> dict:
    return {"available": True, "ok": not drift, "drift": drift, "checked_at": int(time.time()), **facts}


def live_unavailable(reason: str) -> dict:
    return {"available": False, "ok": False, "drift": [], "reason": reason,
            "checked_at": int(time.time())}


def require_hash(payload: dict, key: str, expected: str) -> None:
    if payload.get(key) != expected:
        raise SchemaError(f"payload.{key}: does not match the fields it hashes")


def no_external_live_check(hash_key: str, compute: Callable[[dict], str]) -> Callable[..., dict]:
    """Kinds with no live artifact: the check is that the stored hash still matches its fields."""
    def check(payload: dict, sources: LiveSources) -> dict:
        drift = [] if payload.get(hash_key) == compute(payload) else [f"{hash_key} mismatch"]
        return live_result(drift)
    return check


@dataclass(frozen=True)
class KindSpec:
    kind: str
    park_tag: str  # "awaiting-approval" (an approval gate) or "needs-jack"
    gate: str  # the tag's gate / what: ``[awaiting-approval:<gate>]`` / ``[needs-jack:<gate>]``
    schema_versions: Mapping[int, dict]
    agent_fields: frozenset[str]  # payload keys an agent may supply; the rest are tool fields
    binding_fields: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    executor: Optional[str]  # an executor name in the assistant repo, None = hand back to the owner
    enrich: Callable[[dict, LiveSources], dict]  # agent payload -> full payload (tool fields read live)
    live_check: Callable[[dict, LiveSources], dict]
    legacy_parse: Callable[[str], Optional[dict]]  # free-text park reason -> agent payload, or None
    check: Callable[[dict], None] = lambda env: None  # rules beyond the JSON Schema (hashes, ...)
    # Add-time rules on the agent payload (readability.py), run before the schema so a refusal
    # carries its fix; never run on read, so an item stored before a rule existed still loads.
    precheck: Callable[[dict], None] = lambda payload: None
    # Add-time proof that the payload was written from a complete, current read of its live source
    # (send_message on Slack); runs after the schema, on add and update only, never on read.
    current_check: Callable[[dict, LiveSources], None] = lambda payload, sources: None
    park_params: Callable[[dict], tuple[str, list[tuple[str, str]]]] = lambda env: (env["ask"], [])
    default_then: str = "ready"
    then_values: tuple[str, ...] = ("complete", "ready")
    source_key: Optional[Callable[[dict], str]] = None  # derived and required when set
    default_links: Callable[[dict], list] = field(default=lambda payload: [])
    batchable: bool = False
    # Actions of an item stored under an older payload schema, when they differ from
    # ``allowed_actions``: a stored item keeps the decision path it was parked with.
    schema_actions: Mapping[int, tuple[str, ...]] = field(default_factory=dict)

    @property
    def current_schema(self) -> int:
        return max(self.schema_versions)

    def binding_hash(self, env: dict) -> str:
        payload = env["payload"]
        return prefixed_hash({"kind": self.kind, "schema": env["schema"],
                              "binding": {f: payload.get(f) for f in self.binding_fields}})

    def actions_for(self, payload: dict, schema: int) -> tuple[str, ...]:
        actions = self.schema_actions.get(schema, self.allowed_actions)
        if self.batchable and payload.get("promotion"):
            return tuple(a for a in actions if a != "batch")
        return actions
