"""``send_message`` v1 accepts a ``linkedin`` channel (tools/action_item_kinds/send_message.py).

LinkedIn DMs are drafted by the assistant monitor and sent from Jack's LinkedIn session in the Ego
browser, so a linkedin item carries account ``ego``, the conversation backend id and profile-URL
handles. The pre-LinkedIn channels must validate and hash exactly as before (the §3.2.1 vectors).
"""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from tools.action_item_kinds import LiveSources, SchemaError, build_envelope, item_hash, validate_envelope
from tools.action_item_kinds import send_message
from tools.action_item_kinds.core import STATIC_LINK_HOSTS

THREAD_ID = "2-ZjA3NGQ5YzYtMWM3Ni00ZDQ4LWI2ZjEtY2E3ZTgwZjM1NDQ2XzEwMA=="
PROFILE = "https://www.linkedin.com/in/ann-lee-12345/"
URN_PROFILE = "https://www.linkedin.com/in/ACoAABcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
ENVELOPE = {"title": "Reply to Ann on LinkedIn", "summary": "Ann asked about Thursday. A reply is drafted.",
            "ask": "Send this LinkedIn reply to Ann?", "why": "Ann is waiting on a time.",
            "risk": {"level": "low", "note": "One DM; nothing is sent until you approve."}}


def linkedin_payload(**overrides) -> dict:
    payload = {"channel": "linkedin", "account": "ego", "conversation_id": THREAD_ID,
               "contact": {"name": "Ann Lee", "handle": PROFILE}, "recipients": [{"handle": PROFILE}],
               "last_messages": [{"from": "them", "at": "2026-11-20T09:00:00-06:00", "text": "Thursday?"}],
               "last_inbound_at": "2026-11-20T09:00:00-06:00", "draft_text": "Thursday works.",
               "draft_ref": None}
    return {**payload, **overrides}


def build(payload: dict) -> dict:
    # The real static allowlist, so the default LinkedIn link is proven allowed where items are checked.
    return build_envelope("send_message", payload=payload, sources=LiveSources(), hosts=STATIC_LINK_HOSTS,
                          then="complete", **ENVELOPE)


def test_a_linkedin_item_is_accepted_with_its_source_key_and_thread_link():
    env = build(linkedin_payload())
    assert env["source_key"] == f"linkedin:{THREAD_ID}"
    assert env["links"] == [{"label": "LinkedIn thread",
                             "url": f"https://www.linkedin.com/messaging/thread/{THREAD_ID}/"}]
    validate_envelope(env, STATIC_LINK_HOSTS)
    # The urn form LinkedIn returns for a profile is a handle too.
    build(linkedin_payload(recipients=[{"handle": URN_PROFILE}]))


def test_the_content_hash_is_stable_across_a_trailing_slash_on_the_handle():
    with_slash = build(linkedin_payload())
    bare = PROFILE.rstrip("/")
    without = build(linkedin_payload(contact={"handle": bare}, recipients=[{"handle": bare}]))
    assert with_slash["payload"]["content_hash"] == without["payload"]["content_hash"]
    # Only the scheme and host fold; the slug keeps its case (a urn is case-sensitive).
    assert send_message.normal_handle("https://WWW.LinkedIn.com/in/Ann/") == "https://www.linkedin.com/in/Ann"
    assert send_message.normal_handle(URN_PROFILE.lower()) != send_message.normal_handle(URN_PROFILE)


@pytest.mark.parametrize("overrides, field", [
    ({"account": "jack@cybership.io"}, "payload.account"),
    ({"account": "this-mac"}, "payload.account"),
    ({"conversation_id": f"https://www.linkedin.com/messaging/thread/{THREAD_ID}/"}, "payload.conversation_id"),
    ({"conversation_id": "2-abc/def=="}, "payload.conversation_id"),
    ({"conversation_id": "short"}, "payload.conversation_id"),
    ({"contact": {"handle": "ann@example.com"}}, "payload.contact.handle"),
    ({"recipients": [{"handle": "+15125550147"}]}, "payload.recipients[0].handle"),
    ({"recipients": [{"handle": "https://evil.example/in/ann-lee"}]}, "payload.recipients[0].handle"),
    ({"recipients": [{"handle": "https://linkedin.com/in/ann-lee"}]}, "payload.recipients[0].handle"),
    ({"recipients": [{"handle": "https://www.linkedin.com/company/acme/"}]}, "payload.recipients[0].handle"),
    ({"recipients": [{"handle": f"{PROFILE}?trk=x"}]}, "payload.recipients[0].handle"),
])
def test_an_invalid_linkedin_payload_is_refused_naming_the_field(overrides, field):
    with pytest.raises(SchemaError, match=re.escape(field)):
        build(linkedin_payload(**overrides))


def test_a_linkedin_source_key_with_an_account_part_is_refused():
    env = build(linkedin_payload())
    env["source_key"] = f"linkedin:ego:{THREAD_ID}"
    with pytest.raises(SchemaError, match="source_key"):
        validate_envelope(env, STATIC_LINK_HOSTS)


def test_pre_linkedin_handles_and_a_slack_item_hash_as_before():
    for handle in ("U01ABCDEF", "+15125550147", "Dave@Example.com"):
        assert send_message.normal_handle(handle) == (handle.lower() if "@" in handle else handle)
    at = "2026-11-10T09:00:00-06:00"
    slack = {"channel": "slack", "account": "T07LES81AAE", "conversation_id": "D07ABC123:1794390000.123456",
             "contact": {"name": "Ann", "handle": "U01ABCDEF"}, "recipients": [{"handle": "U01ABCDEF"}],
             "last_messages": [{"from": "them", "at": at, "text": "Thursday?"}], "last_inbound_at": at,
             "draft_text": "Yes, Thursday works.\nI'll send the invite.", "draft_ref": None}

    class SlackRead(LiveSources):
        def slack_conversation(self, channel_id, thread_ts, oldest):
            ts = f"{datetime.fromisoformat(at).timestamp():.6f}"
            return {"thread": [{"ts": ts, "user": "U01ABCDEF"}], "channel": []}

    env = build_envelope("send_message", payload=slack, sources=SlackRead(), hosts=STATIC_LINK_HOSTS,
                         then="complete", **ENVELOPE)
    assert env["payload"]["content_hash"] == \
        "sha256:5672fb1504668d1fb7a1a9c7625f9af0f799b253419614249b19b53f3da3e0c5"
    # The same build on the base commit (before linkedin) gives this item hash: a slack item an
    # agent parked before this change keeps its binding, so Jack's approval of it stays valid.
    assert item_hash(env) == "c5100a959b617e13"
    validate_envelope(env, STATIC_LINK_HOSTS)
