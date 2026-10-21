"""User-scoped Slack events must not enter Hermes's bot reply path."""

import pytest

from plugins.platforms.slack.adapter import SlackAdapter


def test_user_only_authorization_is_passive_delivery():
    payload = {"authorizations": [{"user_id": "U_JACK", "is_bot": False}]}
    assert SlackAdapter._user_only_event_owner_id(payload) == "U_JACK"


def test_bot_authorization_is_not_passive_delivery():
    payload = {"authorizations": [{"user_id": "U_HERMES", "is_bot": True}]}
    assert SlackAdapter._user_only_event_owner_id(payload) == ""


def test_mixed_authorizations_keep_bot_reply_path():
    payload = {
        "authorizations": [
            {"user_id": "U_JACK", "is_bot": False},
            {"user_id": "U_HERMES", "is_bot": True},
        ]
    }
    assert SlackAdapter._user_only_event_owner_id(payload) == ""


@pytest.mark.asyncio
async def test_user_only_event_is_relayed_before_reply_path(monkeypatch):
    adapter = object.__new__(SlackAdapter)
    relayed = []

    async def relay(event, payload):
        relayed.append((event, payload))

    adapter._relay_owner_user_event = relay
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U_JACK")
    event = {"type": "message", "user": "U_COWORKER", "channel": "D123", "ts": "1.2"}
    payload = {"authorizations": [{"user_id": "U_JACK", "is_bot": False}]}

    await adapter._handle_slack_message(event, payload)

    assert relayed == [(event, payload)]


@pytest.mark.asyncio
async def test_unconfigured_user_event_fails_closed_before_reply_path(monkeypatch):
    adapter = object.__new__(SlackAdapter)
    relayed = []

    async def relay(event, payload):
        relayed.append((event, payload))

    adapter._relay_owner_user_event = relay
    monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)
    event = {"type": "message", "user": "U_COWORKER", "channel": "D123", "ts": "1.3"}
    payload = {"authorizations": [{"user_id": "U_JACK", "is_bot": False}]}

    await adapter._handle_slack_message(event, payload)

    assert relayed == []


@pytest.mark.asyncio
async def test_multiple_configured_owners_fail_closed(monkeypatch):
    adapter = object.__new__(SlackAdapter)
    relayed = []

    async def relay(event, payload):
        relayed.append((event, payload))

    adapter._relay_owner_user_event = relay
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U_JACK,U_OTHER")
    event = {"type": "message", "user": "U_COWORKER", "channel": "D123", "ts": "1.4"}
    payload = {"authorizations": [{"user_id": "U_JACK", "is_bot": False}]}

    await adapter._handle_slack_message(event, payload)

    assert relayed == []
