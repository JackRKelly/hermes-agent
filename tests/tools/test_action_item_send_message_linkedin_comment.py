"""``send_message`` v1 accepts a ``linkedin_comment`` channel (tools/action_item_kinds/send_message.py).

A reply to a comment on Jack's LinkedIn post is a PUBLIC post, drafted by the assistant LinkedIn
monitor and sent from Jack's session in the Ego browser after his verified tap. The item carries
account ``ego``, ``<activity id>-<comment id>`` as its destination and the commenter's profile URL
as its one recipient. Items of every earlier channel must validate and hash exactly as before.
"""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from tools.action_item_kinds import (
    KINDS, LiveSources, SchemaError, build_envelope, item_hash, park_reason, validate_envelope,
)
from tools.action_item_kinds.core import STATIC_LINK_HOSTS
from tools.action_item_dedupe import dedupe_key

ACTIVITY, COMMENT = "7265012345678901234", "7265098765432109876"
CONVERSATION = f"{ACTIVITY}-{COMMENT}"
PROFILE = "https://www.linkedin.com/in/ann-lee-12345/"
ENVELOPE = {"title": "Reply to Ann's comment",
            "summary": "Ann asked a question under your post. A public reply is drafted.",
            "ask": "Post this public reply to Ann's comment?", "why": "Ann's question is public and unanswered.",
            "risk": {"level": "medium", "note": "A public comment reply; nothing posts until you approve."}}
# Computed from the base commit (live/upstream-2bd378ed1b at #58, before linkedin_comment) with
# ENVELOPE above, by building the two items in test_earlier_channel_items_hash_as_before.
LINKEDIN_CONTENT_HASH = "sha256:f0b683c4a183eb5e16869330ccb42aefea24dc947c1c1bc7b80a28d0826bfdab"
LINKEDIN_ITEM_HASH = "30df114c83d8f224"
SLACK_CONTENT_HASH = "sha256:5672fb1504668d1fb7a1a9c7625f9af0f799b253419614249b19b53f3da3e0c5"
SLACK_ITEM_HASH = "a18f5c33d514c8f2"


def comment_payload(**overrides) -> dict:
    payload = {"channel": "linkedin_comment", "account": "ego", "conversation_id": CONVERSATION,
               "contact": {"name": "Ann Lee", "handle": PROFILE}, "recipients": [{"handle": PROFILE}],
               "last_messages": [{"from": "them", "name": "Ann Lee", "at": "2026-11-20T09:00:00-06:00",
                                  "text": "Does this work for LTL too?"}],
               "last_inbound_at": "2026-11-20T09:00:00-06:00", "draft_text": "It does, LTL and parcel both.",
               "draft_ref": None}
    return {**payload, **overrides}


def build(payload: dict) -> dict:
    # The real static allowlist, so the default comment link is proven allowed where items are checked.
    return build_envelope("send_message", payload=payload, sources=LiveSources(), hosts=STATIC_LINK_HOSTS,
                          then="complete", **ENVELOPE)


def test_a_comment_reply_is_accepted_with_its_source_key_and_comment_link():
    env = build(comment_payload())
    assert env["source_key"] == f"linkedin_comment:{CONVERSATION}"
    assert env["links"] == [{"label": "LinkedIn comment", "url": (
        f"https://www.linkedin.com/feed/update/urn:li:activity:{ACTIVITY}/"
        f"?commentUrn=urn%3Ali%3Acomment%3A%28activity%3A{ACTIVITY}%2C{COMMENT}%29")}]
    validate_envelope(env, STATIC_LINK_HOSTS)
    assert "channel=linkedin_comment" in park_reason(env)
    # One open item per comment, never shared with the DM thread or another comment.
    assert dedupe_key("send_message", env["payload"], "", "b", "c", "t") == f"message:linkedin_comment:{CONVERSATION}"


def test_the_hash_binds_the_comment_and_folds_only_the_handle_host_and_slash():
    env = build(comment_payload())
    bare = build(comment_payload(contact={"handle": PROFILE.rstrip("/")}, recipients=[{"handle": PROFILE.rstrip("/")}]))
    assert env["payload"]["content_hash"] == bare["payload"]["content_hash"]
    other = build(comment_payload(conversation_id=f"{ACTIVITY}-7265098765432109877"))
    assert other["payload"]["content_hash"] != env["payload"]["content_hash"]
    # The same reply as a DM is a different destination, so a different hash.
    dm = dict(env["payload"], channel="linkedin")
    assert KINDS["send_message"].enrich({k: v for k, v in dm.items() if k != "content_hash"},
                                        LiveSources())["content_hash"] != env["payload"]["content_hash"]


@pytest.mark.parametrize("overrides, field", [
    ({"account": "jack@cybership.io"}, "payload.account"),
    ({"conversation_id": "urn:li:activity:7265012345678901234"}, "payload.conversation_id"),
    ({"conversation_id": f"https://www.linkedin.com/feed/update/urn:li:activity:{ACTIVITY}/"},
     "payload.conversation_id"),
    ({"conversation_id": ACTIVITY}, "payload.conversation_id"),
    ({"conversation_id": f"{ACTIVITY}:{COMMENT}"}, "payload.conversation_id"),
    ({"conversation_id": f"12345-{COMMENT}"}, "payload.conversation_id"),
    ({"recipients": [{"handle": "ann@example.com"}]}, "payload.recipients[0].handle"),
    ({"recipients": [{"handle": "https://www.linkedin.com/in/someone-else/"}]}, "payload.recipients[0].handle"),
    ({"recipients": [{"handle": PROFILE}, {"handle": "https://www.linkedin.com/in/bob/"}]}, "payload.recipients"),
    ({"last_messages": [{"from": "them", "at": "2026-11-20T09:00:00-06:00", "text": f"m{i}"} for i in range(4)]},
     "payload.last_messages"),
])
def test_an_invalid_comment_reply_is_refused_naming_the_field(overrides, field):
    with pytest.raises(SchemaError, match=re.escape(field)):
        build(comment_payload(**overrides))


def test_the_comment_plus_two_earlier_replies_is_accepted():
    thread = [{"from": "them", "at": "2026-11-20T08:00:00-06:00", "text": "Nice post."},
              {"from": "jack", "at": "2026-11-20T08:30:00-06:00", "text": "Thanks!"},
              {"from": "them", "at": "2026-11-20T09:00:00-06:00", "text": "Does this work for LTL too?"}]
    build(comment_payload(last_messages=thread))


def test_the_live_check_fails_closed_with_no_linkedin_reader():
    env = build(comment_payload())
    live = KINDS["send_message"].live_check(env["payload"], LiveSources())
    assert live["available"] is False and live["ok"] is False
    assert KINDS["send_message"].executor is None


def test_earlier_channel_items_hash_as_before():
    """Pinned on the base commit (before linkedin_comment): an item parked before this change keeps
    its binding, so Jack's approval of it stays valid."""
    linkedin = {"channel": "linkedin", "account": "ego",
                "conversation_id": "2-ZjA3NGQ5YzYtMWM3Ni00ZDQ4LWI2ZjEtY2E3ZTgwZjM1NDQ2XzEwMA==",
                "contact": {"name": "Ann Lee", "handle": PROFILE}, "recipients": [{"handle": PROFILE}],
                "last_messages": [{"from": "them", "at": "2026-11-20T09:00:00-06:00", "text": "Thursday?"}],
                "last_inbound_at": "2026-11-20T09:00:00-06:00", "draft_text": "Thursday works.", "draft_ref": None}
    env = build(linkedin)
    assert env["payload"]["content_hash"] == LINKEDIN_CONTENT_HASH
    assert item_hash(env) == LINKEDIN_ITEM_HASH

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
    assert env["payload"]["content_hash"] == SLACK_CONTENT_HASH
    assert item_hash(env) == SLACK_ITEM_HASH
