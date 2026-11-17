"""``send_email`` v1, FROZEN (design §3.2.1): byte-compatible with assistant ``scripts/action_item.py``.

The draft fields (recipients, subject, body, attachments) are supplied by the producer exactly as
the frozen v1 contract defines; the tool computes ``thread_url`` and ``content_hash`` from them and
refuses a supplied hash that disagrees. hermes-agent has no Gmail reader, so the live re-check is
the send handler's (it recomputes ``content_hash`` from the live draft before sending); a host with
a reader passes it through ``LiveSources.gmail_draft``.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from tools.action_item_kinds.core import SchemaError, prefixed_hash
from tools.action_item_kinds.spec import (
    KindSpec, LiveSources, SourceUnavailable, live_result, live_unavailable, load_versions,
    require_hash,
)
from tools.action_item_kinds import legacy

KIND = "send_email"
PAYLOAD_KEYS = frozenset({
    "account", "thread_id", "thread_url", "draft_id", "from", "last_inbound_at",
    "last_inbound_excerpt", "to", "cc", "bcc", "subject", "body", "attachments", "content_hash",
})


def _addresses(people: Iterable[Any]) -> list[str]:
    return sorted((p["email"] if isinstance(p, dict) else str(p)).strip().lower() for p in people)


def content_hash(to: Iterable[Any], cc: Iterable[Any], bcc: Iterable[Any], subject: str, body: str,
                 attachments: Iterable[dict]) -> str:
    """``sha256:`` over the recipients (sorted, lowercased addresses only), subject, LF body and
    the attachments as ``[{filename, sha256}]`` sorted by ``(filename, sha256)``."""
    files = sorted(({"filename": a["filename"], "sha256": a["sha256"]} for a in attachments),
                   key=lambda a: (a["filename"], a["sha256"]))
    return prefixed_hash({"to": _addresses(to), "cc": _addresses(cc), "bcc": _addresses(bcc),
                          "subject": subject, "body": body.replace("\r\n", "\n"), "attachments": files})


def payload_content_hash(p: dict) -> str:
    return content_hash(p["to"], p["cc"], p["bcc"], p["subject"], p["body"], p["attachments"])


def thread_url(account: str, thread_id: str) -> str:
    return f"https://mail.google.com/mail/u/{account}/#all/{thread_id}"


def _enrich(agent: dict, sources: LiveSources) -> dict:
    payload = dict(agent)
    if "account" in payload and "thread_id" in payload:
        payload.setdefault("thread_url", thread_url(payload["account"], payload["thread_id"]))
    if "content_hash" not in payload:
        try:
            payload["content_hash"] = payload_content_hash(payload)
        except (KeyError, TypeError, AttributeError) as exc:
            raise SchemaError(f"payload: cannot hash the draft fields ({exc!r})") from exc
    return payload


def _check(env: dict) -> None:
    p = env["payload"]
    if p["thread_url"] != thread_url(p["account"], p["thread_id"]):
        raise SchemaError("payload.thread_url: must be " + thread_url(p["account"], p["thread_id"]))
    require_hash(p, "content_hash", payload_content_hash(p))
    if env["source_key"] != _source_key(p):
        raise SchemaError(f"source_key: must be {_source_key(p)}")


def _source_key(p: dict) -> str:
    return f"gmail:{p.get('account')}:{p.get('thread_id')}"


def _park(env: dict) -> tuple[str, list[tuple[str, str]]]:
    p = env["payload"]
    return p["thread_url"], [("draft", p["draft_id"]), ("thread", p["thread_id"]), ("account", p["account"])]


def _live_check(payload: dict, sources: LiveSources) -> dict:
    try:
        draft = sources.gmail_draft(payload["account"], payload["draft_id"])
    except SourceUnavailable as exc:
        return live_unavailable(str(exc))
    if draft is None:
        return live_result(["draft is gone"])
    live_hash = payload_content_hash(draft)
    return live_result([] if live_hash == payload["content_hash"] else ["draft changed"], live_hash=live_hash)


def _legacy(reason: str) -> Optional[dict]:
    gate = legacy.parse_gate(reason)
    if not gate or gate[0] != "send-email":
        return None
    params = gate[2]
    if not all(params.get(k) for k in ("draft", "thread", "account")):
        return None
    return {"account": params["account"], "thread_id": params["thread"], "draft_id": params["draft"],
            "thread_url": thread_url(params["account"], params["thread"])}


SPEC = KindSpec(
    kind=KIND, park_tag="awaiting-approval", gate="send-email",
    schema_versions=load_versions(KIND, 1), agent_fields=PAYLOAD_KEYS,
    binding_fields=("account", "draft_id", "thread_id", "content_hash"),
    allowed_actions=("approve", "reject", "edit", "snooze", "later"),
    executor="send-email-handler", enrich=_enrich, live_check=_live_check, legacy_parse=_legacy,
    check=_check, park_params=_park, default_then="complete", then_values=("complete",),
    source_key=_source_key,
    default_links=lambda p: [{"label": "Gmail thread", "url": p.get("thread_url", "")}],
)
