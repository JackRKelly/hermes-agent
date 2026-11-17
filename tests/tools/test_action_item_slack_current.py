"""A Slack ``send_message`` is parked only from a complete, current read of the conversation.

Regression: an agent parked a Slack reply after reading only the thread (``conversations.replies``);
the customer had followed up top-level in the channel and a teammate and Jack had already answered,
so Jack approved from a stale preview. ``action_item_add`` / ``action_item_update`` now re-read the
thread AND the channel history since the earliest ``last_messages`` entry and refuse when any
message, from anyone, is missing; an unreadable conversation refuses too.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tools.action_item_kinds import LiveSources, SchemaError, build_envelope
from tools.action_item_kinds.slack import SlackSources
from tools.action_item_kinds.spec import SourceUnavailable

HOSTS = {"github.com", "linear.app", "mail.google.com", "app.slack.com"}
ENVELOPE = {"title": "Reply to Ann about Thursday", "summary": "Ann asked about Thursday. A reply is drafted.",
            "ask": "Send this Slack reply?", "why": "Ann is waiting.", "risk": {"level": "low", "note": "One reply."},
            "then": "complete"}
CHANNEL, ROOT = "C07ABC123", 1794390000.123456
CUSTOMER, TEAMMATE, JACK = "U01ANN", "U02BOB", "U03JACK"


def iso(ts: float) -> str:
    """``at`` as an agent writes it: cut to the second, in Jack's offset."""
    return datetime.fromtimestamp(int(ts), UTC).astimezone().isoformat()


def msg(ts: float, user: str, text: str, **extra) -> dict:
    return {"ts": f"{ts:.6f}", "user": user, "text": text, **extra}


def entry(ts: float, text: str, who: str = "them") -> dict:
    return {"from": who, "at": iso(ts), "text": text}


class FakeSlack(LiveSources):
    def __init__(self, thread=(), channel=(), error: str | None = None):
        self.thread, self.channel, self.error, self.reads = list(thread), list(channel), error, []

    def slack_conversation(self, channel_id, thread_ts, oldest):
        self.reads.append((channel_id, thread_ts, oldest))
        if self.error:
            raise SourceUnavailable(self.error)
        return {"thread": [m for m in self.thread if float(m["ts"]) >= oldest],
                "channel": [m for m in self.channel if float(m["ts"]) >= oldest]}


ROOT_MSG = msg(ROOT, CUSTOMER, "Can we move the pickup to Thursday?", user_profile={"real_name": "Ann Customer"})


def payload(last_messages: list[dict], last_inbound_at: str | None = None) -> dict:
    inbound = [m["at"] for m in last_messages if m["from"] == "them"]
    return {"channel": "slack", "account": "T07LES81AAE", "conversation_id": f"{CHANNEL}:{ROOT:.6f}",
            "contact": {"name": "Ann", "handle": CUSTOMER}, "recipients": [{"handle": CUSTOMER}],
            "last_messages": last_messages, "last_inbound_at": last_inbound_at or max(inbound),
            "draft_text": "Yes, Thursday works.", "draft_ref": None}


def build(p: dict, sources: LiveSources) -> dict:
    return build_envelope("send_message", payload=p, sources=sources, hosts=HOSTS, **ENVELOPE)


def test_a_complete_read_of_thread_and_channel_is_accepted():
    reply, top = ROOT + 60.5, ROOT + 120.25
    slack = FakeSlack(thread=[ROOT_MSG, msg(reply, CUSTOMER, "Any update?")],
                      channel=[ROOT_MSG, msg(top, CUSTOMER, "Also, 3 pallets not 2"),
                               msg(top + 1, CUSTOMER, "joined", subtype="channel_join")])
    env = build(payload([entry(ROOT, "Can we move the pickup to Thursday?"), entry(reply, "Any update?"),
                         entry(top, "Also, 3 pallets not 2")]), slack)
    assert env["payload"]["last_messages"][-1]["text"] == "Also, 3 pallets not 2"
    assert slack.reads == [(CHANNEL, f"{ROOT:.6f}", int(ROOT) - 1.0)]


def test_a_thread_only_read_missing_a_newer_reply_is_refused():
    late = ROOT + 300.0
    slack = FakeSlack(thread=[ROOT_MSG, msg(late, CUSTOMER, "Hello? Need an answer today",
                                              user_profile={"real_name": "Ann Customer"})], channel=[ROOT_MSG])
    with pytest.raises(SchemaError) as err:
        build(payload([entry(ROOT, "Can we move the pickup to Thursday?")]), slack)
    text = str(err.value)
    assert text.startswith("payload.last_messages: the Slack conversation has 1 message(s)")
    assert f"Ann Customer ({CUSTOMER}) at {datetime.fromtimestamp(late, UTC).isoformat(timespec='seconds')}" \
        in text and "in the thread" in text and '"Hello? Need an answer today"' in text
    assert "conversations.replies" in text and "conversations.history" in text


def test_a_newer_top_level_channel_message_is_refused_even_when_the_thread_is_complete():
    reply, top = ROOT + 60.0, ROOT + 900.0
    slack = FakeSlack(thread=[ROOT_MSG, msg(reply, CUSTOMER, "Any update?")],
                      channel=[ROOT_MSG, msg(top, CUSTOMER, "Never mind, we booked Friday instead")])
    with pytest.raises(SchemaError, match=r"payload\.last_messages: .* in the channel: \"Never mind, we booked"):
        build(payload([entry(ROOT, "Can we move the pickup to Thursday?"), entry(reply, "Any update?")]), slack)


def test_a_teammate_and_jack_reply_already_sent_are_refused():
    mate, jack = ROOT + 30.0, ROOT + 45.0
    slack = FakeSlack(thread=[ROOT_MSG, msg(mate, TEAMMATE, "On it, booked for Thursday 10am"),
                              msg(jack, JACK, "Confirmed, see you Thursday")], channel=[ROOT_MSG])
    with pytest.raises(SchemaError) as err:
        build(payload([entry(ROOT, "Can we move the pickup to Thursday?")]), slack)
    assert "has 2 message(s)" in str(err.value)
    assert TEAMMATE in str(err.value) and JACK in str(err.value)


@pytest.mark.parametrize("error", ["no SLACK_USER_TOKEN in this profile's secrets", "conversations.history: ratelimited"])
def test_an_unreadable_conversation_refuses_instead_of_parking_unverified(error):
    with pytest.raises(SchemaError, match="payload.last_messages: cannot verify the Slack conversation is current"):
        build(payload([entry(ROOT, "Can we move the pickup to Thursday?")]), FakeSlack(error=error))


def test_an_entry_matching_no_message_is_refused():
    """Else a fabricated ``at`` after the newest message would make the window empty and pass."""
    slack = FakeSlack(thread=[ROOT_MSG], channel=[ROOT_MSG])
    with pytest.raises(SchemaError, match=r"payload\.last_messages\[1\]\.at: no message in the Slack thread"):
        build(payload([entry(ROOT, "Can we move the pickup to Thursday?"), entry(ROOT + 9999, "made up")]), slack)


def test_last_inbound_at_must_be_the_newest_message_not_from_jack():
    jack = ROOT + 45.0
    slack = FakeSlack(thread=[ROOT_MSG, msg(jack, JACK, "Checking")], channel=[ROOT_MSG])
    lines = [entry(ROOT, "Can we move the pickup to Thursday?"), entry(jack, "Checking", who="jack")]
    with pytest.raises(SchemaError, match="payload.last_inbound_at: must be the newest message not from Jack"):
        build(payload(lines, last_inbound_at=iso(jack)), slack)
    assert build(payload(lines), slack)


def test_other_channels_are_not_read():
    p = {"channel": "sms", "account": "this-mac", "conversation_id": "SMS;-;+15125550199",
         "contact": {"handle": "+15125550199"}, "recipients": [{"handle": "+15125550199"}],
         "last_messages": [{"from": "them", "at": "2026-11-10T09:00:00-06:00", "text": "eta?"}],
         "last_inbound_at": "2026-11-10T09:00:00-06:00", "draft_text": "On my way", "draft_ref": None}
    assert build(p, FakeSlack(error="must not be read"))


# --- the reader --------------------------------------------------------------------------------

def test_the_reader_pages_both_reads_and_fails_closed_on_a_slack_error():
    calls = []

    def api(method, params):
        calls.append((method, params.get("cursor")))
        if method == "conversations.replies":
            return {"ok": True, "messages": [{"ts": "1.000000"}],
                    "response_metadata": {"next_cursor": "" if params.get("cursor") else "p2"}}
        return {"ok": True, "messages": [{"ts": "2.000000"}]}

    read = SlackSources(api).slack_conversation(CHANNEL, f"{ROOT:.6f}", 0.5)
    assert len(read["thread"]) == 2 and len(read["channel"]) == 1
    assert calls == [("conversations.replies", None), ("conversations.replies", "p2"), ("conversations.history", None)]
    with pytest.raises(SourceUnavailable, match="conversations.replies: channel_not_found"):
        SlackSources(lambda m, p: {"ok": False, "error": "channel_not_found"}).slack_conversation(CHANNEL, "1.0", 0)


def test_the_reader_without_a_token_is_unavailable():
    with pytest.raises(SourceUnavailable, match="no SLACK_USER_TOKEN"):
        SlackSources().slack_conversation(CHANNEL, f"{ROOT:.6f}", 0.5)


# --- end to end through the tool ---------------------------------------------------------------

def test_the_tool_refuses_a_stale_slack_park_and_writes_nothing(monkeypatch, tmp_path):
    from tests.tools.test_action_item_tools import as_worker, call, card_state, make_card
    from hermes_cli import kanban_db as kb
    from tools.registry import invalidate_check_fn_cache

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_DB", "HERMES_PROFILE_NAME", "HERMES_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    invalidate_check_fn_cache()
    late = ROOT + 300.0

    class Slack(FakeSlack):  # what the host's default sources read, as the tool builds them
        def __init__(self):
            super().__init__(thread=[ROOT_MSG], channel=[ROOT_MSG, msg(late, CUSTOMER, "Hello?")])

    class NoGh(LiveSources):
        pass

    monkeypatch.setattr("tools.action_item_kinds.slack.SlackSources", Slack)
    monkeypatch.setattr("tools.action_item_kinds.github.GhSources", NoGh)
    tid, run_id = make_card("support-captain", claim=True)
    as_worker(monkeypatch, "support-captain", tid, run_id)
    stale = payload([entry(ROOT, "Can we move the pickup to Thursday?")])
    out = call("action_item_add", kind="send_message", payload=stale, **ENVELOPE)
    assert "payload.last_messages: the Slack conversation has 1 message(s)" in out["error"], out
    status, comments, events = card_state(tid)
    assert status == "running" and comments == [] and not [e for e in events if e.kind == "blocked"]
    fresh = payload([entry(ROOT, "Can we move the pickup to Thursday?"), entry(late, "Hello?")])
    assert call("action_item_add", kind="send_message", payload=fresh, **ENVELOPE)["ok"]
