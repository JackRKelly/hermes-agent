"""The Slack reader behind ``send_message``'s add-time check.

The token is the profile's own ``SLACK_USER_TOKEN`` when it has one, else the shared Slack reader:
the ``SLACK_USER_TOKEN`` line of the Hermes ROOT home's ``.env``, the user token the assistant's
Slack monitors and helpers already read with. Named profiles under ``<root>/profiles/`` carry no
token of their own (copying one into them is not allowed), so without the fallback every crew
worker's Slack ``send_message`` add refused as unverifiable. A routed profile (multiplexing
gateway, a foreign-home scope) never falls back: its scope is deliberately blind to other homes'
secrets.

Read-only (``conversations.replies`` and ``conversations.history`` GETs); nothing here posts. The
token, wherever it came from, lives only in this call's closure: it is never written to a ``.env``,
``os.environ`` or a secret scope, and never appears in an error or a log line. A missing token, an
HTTP failure or a Slack ``ok: false`` is ``SourceUnavailable`` so the caller fails closed.
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


def _shared_reader_token() -> str:
    """The shared Slack reader's token: ``SLACK_USER_TOKEN`` in the Hermes root home's ``.env``.

    Parsed with the one ``.env`` tokenizer (``load_env_file``); a read, never a copy anywhere.
    """
    from agent.secret_scope import load_env_file
    from hermes_constants import get_default_hermes_root

    return load_env_file(get_default_hermes_root() / ".env").get(TOKEN_NAME, "").strip()


class SlackSources(LiveSources):
    """Live Slack reads for ``send_message``; ``api`` injects a fake transport in tests."""

    def __init__(self, api: Optional[Api] = None):
        self._api = api

    def _client(self) -> Api:
        if self._api is not None:
            return self._api
        from agent.secret_scope import get_secret, serves_routed_profile

        # Multiplex with no scope bound still raises UnscopedSecretError here, as before.
        token = get_secret(TOKEN_NAME)
        if token:
            return _http_api(token)
        if serves_routed_profile():
            raise SourceUnavailable(f"no {TOKEN_NAME} in this profile's secrets (a routed profile does "
                                    "not use the shared Slack reader)")
        token = _shared_reader_token()
        if not token:
            raise SourceUnavailable(f"neither this profile nor the shared Slack reader has {TOKEN_NAME}")
        return _http_api(token)

    def slack_conversation(self, channel_id: str, thread_ts: str, oldest: float) -> dict[str, Any]:
        api = self._client()
        window = {"channel": channel_id, "oldest": f"{oldest:.6f}", "inclusive": "true"}
        return {"thread": _pages(api, "conversations.replies", {**window, "ts": thread_ts}),
                "channel": _pages(api, "conversations.history", window)}
