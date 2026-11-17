"""The Slack reader behind ``send_message``'s add-time check: the profile's ``SLACK_USER_TOKEN``.

Read-only (``conversations.replies`` and ``conversations.history`` GETs); nothing here posts. The
token is the same user token the assistant's Slack monitors read with; it never appears in an error
or a log line. A missing token, an HTTP failure or a Slack ``ok: false`` is ``SourceUnavailable``
so the caller fails closed.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from tools.action_item_kinds.spec import LiveSources, SourceUnavailable

SLACK_API = "https://slack.com/api/"
TOKEN_NAME = "SLACK_USER_TOKEN"
TIMEOUT_SECONDS = 15
PAGE_LIMIT = 200
# A window wider than this many pages is not a conversation an agent read message by message.
MAX_PAGES = 10

Api = Callable[[str, dict], dict]


def _http_api(token: str) -> Api:
    def call(method: str, params: dict) -> dict:
        url = SLACK_API + method + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise SourceUnavailable(f"{method} returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise SourceUnavailable(f"{method} failed: {type(exc).__name__}") from exc
    return call


def _pages(api: Api, method: str, params: dict) -> list[dict]:
    messages: list[dict] = []
    cursor = ""
    for _ in range(MAX_PAGES):
        data = api(method, {**params, "limit": PAGE_LIMIT, **({"cursor": cursor} if cursor else {})})
        if not isinstance(data, dict) or not data.get("ok"):
            error = data.get("error") if isinstance(data, dict) else "not a JSON object"
            raise SourceUnavailable(f"{method}: {error}")
        messages.extend(m for m in data.get("messages") or [] if isinstance(m, dict))
        cursor = str((data.get("response_metadata") or {}).get("next_cursor") or "")
        if not cursor:
            return messages
    raise SourceUnavailable(f"{method}: more than {MAX_PAGES * PAGE_LIMIT} messages since the earliest "
                            "last_messages entry")


class SlackSources(LiveSources):
    def __init__(self, api: Optional[Api] = None):
        self._api = api

    def _client(self) -> Api:
        if self._api is not None:
            return self._api
        from agent.secret_scope import get_secret

        token = get_secret(TOKEN_NAME)
        if not token:
            raise SourceUnavailable(f"no {TOKEN_NAME} in this profile's secrets")
        return _http_api(token)

    def slack_conversation(self, channel_id: str, thread_ts: str, oldest: float) -> dict[str, Any]:
        api = self._client()
        window = {"channel": channel_id, "oldest": f"{oldest:.6f}", "inclusive": "true"}
        return {"thread": _pages(api, "conversations.replies", {**window, "ts": thread_ts}),
                "channel": _pages(api, "conversations.history", window)}
