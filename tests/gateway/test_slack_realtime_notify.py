"""Behavior tests for the realtime Slack-to-Telegram relay."""

import importlib.util
from pathlib import Path

SCRIPT = Path.home() / ".hermes" / "scripts" / "slack_realtime_notify.py"
spec = importlib.util.spec_from_file_location("slack_realtime_notify", SCRIPT)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


def test_incoming_human_dm_notifies():
    event = {"user": "U_OTHER", "channel_type": "im", "text": "Can you review this?"}
    assert relay.should_notify(event, "U_OWNER", sender_is_bot=False) is True


def test_owner_message_does_not_notify():
    event = {"user": "U_OWNER", "channel_type": "im", "text": "My reply"}
    assert relay.should_notify(event, "U_OWNER", sender_is_bot=False) is False


def test_channel_message_requires_exact_mention_or_owner_thread():
    ordinary = {"user": "U_OTHER", "channel_type": "channel", "text": "hello"}
    mention = {"user": "U_OTHER", "channel_type": "channel", "text": "hey <@U_OWNER>"}
    assert relay.should_notify(ordinary, "U_OWNER", sender_is_bot=False) is False
    assert relay.should_notify(mention, "U_OWNER", sender_is_bot=False) is True
    assert relay.should_notify(ordinary, "U_OWNER", sender_is_bot=False, root_author_id="U_OWNER") is True


def test_bot_message_does_not_notify():
    event = {"user": "U_BOT", "channel_type": "im", "text": "alert"}
    assert relay.should_notify(event, "U_OWNER", sender_is_bot=True) is False
