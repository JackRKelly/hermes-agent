"""A user-token ``message.im`` subscription also delivers the owner's DMs with other people.

Those DMs are not conversations with the bot: the bot token cannot see them (``channel_not_found``),
cannot reply there, and must not start an agent turn on them. The owner's own DM with the bot
(``is_im: true``) still works, and an unclassified API error fails open.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter


class _SlackApiError(Exception):
    def __init__(self, error: str):
        super().__init__(error)
        self.response = {"ok": False, "error": error}


@pytest.fixture(autouse=True)
def _redirect_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache")


@pytest.fixture
def adapter():
    a = SlackAdapter(PlatformConfig(enabled=True, token="***"))
    a._app = MagicMock()
    a._app.client = AsyncMock()
    a._bot_user_id = "U_BOT"
    a._running = True
    a.handle_message = AsyncMock()
    return a


def _dm(channel: str, ts: str = "1700000000.000001") -> dict:
    return {"channel": channel, "channel_type": "im", "user": "U_OWNER", "text": "did you see my message?", "ts": ts}


@pytest.mark.asyncio
async def test_owner_dm_with_someone_else_never_starts_a_turn(adapter):
    adapter._app.client.conversations_info = AsyncMock(side_effect=_SlackApiError("channel_not_found"))
    await adapter._handle_slack_message(_dm("D_OWNER_AND_COWORKER"))
    await adapter._handle_slack_message(_dm("D_OWNER_AND_COWORKER", ts="1700000000.000002"))
    adapter.handle_message.assert_not_called()
    # The verdict is cached: one lookup for the channel, not one per message.
    assert adapter._app.client.conversations_info.await_count == 1


@pytest.mark.asyncio
async def test_owner_dm_with_the_bot_still_works(adapter):
    adapter._app.client.conversations_info = AsyncMock(
        return_value={"ok": True, "channel": {"id": "D_BOT", "is_im": True, "user": "U_OWNER"}})
    assert await adapter._bot_is_dm_party("D_BOT") is True


@pytest.mark.asyncio
async def test_mpim_the_bot_is_not_in_is_dropped(adapter):
    adapter._app.client.conversations_info = AsyncMock(
        return_value={"ok": True, "channel": {"id": "G_MPIM", "is_mpim": True, "is_member": False}})
    assert await adapter._bot_is_dm_party("G_MPIM") is False


@pytest.mark.asyncio
async def test_transient_error_fails_open_and_is_not_cached(adapter):
    adapter._app.client.conversations_info = AsyncMock(side_effect=_SlackApiError("ratelimited"))
    assert await adapter._bot_is_dm_party("D_BOT") is True
    assert ("", "D_BOT") not in adapter._dm_bot_party_cache
