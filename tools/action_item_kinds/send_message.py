"""``send_message`` v1, FROZEN (design §3.2.1): byte-compatible with assistant ``scripts/action_item.py``.

iMessage, SMS, Google Voice and Slack. The draft lives in the payload (``draft_text``); the tool
computes ``content_hash`` and the default links. Per-channel forms the JSON Schema cannot express
(account, conversation id, handle) are checked in ``_check``.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from tools.action_item_kinds.core import EMAIL_RE, SchemaError, prefixed_hash
from tools.action_item_kinds.spec import (
    KindSpec, LiveSources, SourceUnavailable, live_result, live_unavailable, load_versions,
    require_hash,
)
from tools.action_item_kinds import legacy

KIND = "send_message"
PAYLOAD_KEYS = frozenset({
    "channel", "account", "conversation_id", "contact", "recipients", "last_messages",
    "last_inbound_at", "draft_text", "draft_ref", "content_hash",
})
E164_RE = re.compile(r"^\+[1-9][0-9]{6,14}$")
SLACK_USER_RE = re.compile(r"^[UW][A-Z0-9]{2,}$")
SLACK_TEAM_RE = re.compile(r"^[TE][A-Z0-9]{2,}$")
SLACK_CONVERSATION_RE = re.compile(r"^([CDG][A-Z0-9]{2,}):([0-9]{9,11}\.[0-9]{6})$")


def normal_handle(handle: str) -> str:
    """Hash form of a recipient handle: an email lowercased; a Slack id and an E.164 phone as given."""
    return handle.lower() if "@" in handle else handle


def content_hash(channel: str, conversation_id: str, recipients: Iterable[Any], text: str) -> str:
    handles = sorted(normal_handle(r["handle"] if isinstance(r, dict) else str(r)) for r in recipients)
    return prefixed_hash({"channel": channel, "conversation_id": conversation_id, "recipients": handles,
                          "text": text.replace("\r\n", "\n")})


def source_key(channel: str, account: str, conversation_id: str) -> str:
    return {"imessage": f"imessage:{conversation_id}", "sms": f"sms:{conversation_id}",
            "google_voice": f"gvoice:{account}:{conversation_id}",
            "slack": f"slack:{account}:{conversation_id}"}.get(channel, "")


def slack_link(team: str, conversation_id: str) -> str:
    channel, _, ts = conversation_id.partition(":")
    return f"https://app.slack.com/client/{team}/{channel}/thread/{channel}-{ts}"


def default_links(p: dict) -> list[dict]:
    channel, account = str(p.get("channel") or ""), str(p.get("account") or "")
    conv = str(p.get("conversation_id") or "")
    if channel == "slack":
        return [{"label": "Slack thread", "url": slack_link(account, conv)}]
    if channel == "google_voice":
        return [{"label": "Google Voice thread", "url": f"https://mail.google.com/mail/u/{account}/#all/{conv}"}]
    return []


def _payload_hash(p: dict) -> str:
    return content_hash(p["channel"], p["conversation_id"], p["recipients"], p["draft_text"])


def _enrich(agent: dict, sources: LiveSources) -> dict:
    payload = dict(agent)
    if "content_hash" not in payload:
        try:
            payload["content_hash"] = content_hash(
                str(payload.get("channel") or ""), str(payload.get("conversation_id") or ""),
                payload["recipients"], payload["draft_text"])
        except (KeyError, TypeError, AttributeError) as exc:
            raise SchemaError(f"payload: cannot hash the draft fields ({exc!r})") from exc
    return payload


def _check_handle(value: str, channel: str, where: str) -> None:
    """Slack: a ``U``/``W`` user id. Otherwise an email or an E.164 phone: a national format is
    refused, so verifiers never guess a country."""
    if channel == "slack":
        if not SLACK_USER_RE.match(value):
            raise SchemaError(f"{where}: a Slack user id (U... or W...) ({value!r})")
    elif not (EMAIL_RE.match(value) or E164_RE.match(value)):
        raise SchemaError(f"{where}: an email or an E.164 phone like +15125550147 ({value!r})")


def _check_account(p: dict) -> None:
    channel, account = p["channel"], p["account"]
    if channel == "google_voice" and not EMAIL_RE.match(account):
        raise SchemaError(f"payload.account: not an email address ({account!r})")
    if channel == "slack" and not SLACK_TEAM_RE.match(account):
        raise SchemaError(f"payload.account: Jack's Slack team id for slack ({account!r})")
    if channel in ("imessage", "sms") and account != "this-mac":
        raise SchemaError('payload.account: must be "this-mac" for imessage/sms')


def _check(env: dict) -> None:
    p = env["payload"]
    channel = p["channel"]
    _check_account(p)
    if channel == "slack" and not SLACK_CONVERSATION_RE.match(p["conversation_id"]):
        raise SchemaError("payload.conversation_id: <channel_id>:<thread_ts> for slack")
    if "team" in p["contact"] and channel != "slack":
        raise SchemaError("payload.contact: unknown keys ['team']")
    _check_handle(p["contact"]["handle"], channel, "payload.contact.handle")
    for i, recipient in enumerate(p["recipients"]):
        _check_handle(recipient["handle"], channel, f"payload.recipients[{i}].handle")
    require_hash(p, "content_hash", _payload_hash(p))
    expected = source_key(channel, p["account"], p["conversation_id"])
    if env["source_key"] != expected:
        raise SchemaError(f"source_key: must be {expected}")


def _park(env: dict) -> tuple[str, list[tuple[str, str]]]:
    p = env["payload"]
    target = env["links"][0]["url"] if env["links"] else p["conversation_id"]
    return target, [("channel", p["channel"]), ("conversation", p["conversation_id"])]


def _live_check(payload: dict, sources: LiveSources) -> dict:
    if payload.get("content_hash") != _payload_hash(payload):
        return live_result(["content_hash mismatch"])
    try:
        thread = sources.conversation(payload["channel"], payload["account"], payload["conversation_id"])
    except SourceUnavailable as exc:
        return live_unavailable(str(exc))
    newer = str(thread.get("last_inbound_at") or "") > str(payload["last_inbound_at"])
    return live_result(["newer inbound message"] if newer else [])


def _legacy(reason: str) -> Optional[dict]:
    gate = legacy.parse_gate(reason)
    if not gate or gate[0] != "send-message":
        return None
    params = gate[2]
    if not (params.get("channel") and params.get("conversation")):
        return None
    return {"channel": params["channel"], "conversation_id": params["conversation"]}


SPEC = KindSpec(
    kind=KIND, park_tag="awaiting-approval", gate="send-message",
    schema_versions=load_versions(KIND, 1), agent_fields=PAYLOAD_KEYS,
    binding_fields=("channel", "account", "conversation_id", "draft_ref", "content_hash"),
    allowed_actions=("approve", "reject", "edit", "snooze", "later"),
    executor=None, enrich=_enrich, live_check=_live_check, legacy_parse=_legacy,
    check=_check, park_params=_park, default_then="complete", then_values=("complete",),
    source_key=lambda p: source_key(str(p.get("channel") or ""), str(p.get("account") or ""),
                                    str(p.get("conversation_id") or "")),
    default_links=default_links,
)
