"""A named profile verifies a Slack ``send_message`` through the shared Slack reader.

Regression: a planner worker (a profile under ``<root>/profiles/``) called
``action_item_add(kind="send_message", channel="slack")`` and was refused with "cannot verify the
Slack conversation is current (no SLACK_USER_TOKEN in this profile's secrets)", although the shared
helper reads Slack with the ``SLACK_USER_TOKEN`` in the Hermes root ``.env``. ``SlackSources`` now
falls back to that root token when the profile has none, read in-process only; a routed profile
never falls back, and every unreadable conversation still refuses.
"""
from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path

import pytest

from tests.tools.test_action_item_slack_current import (
    CHANNEL, CUSTOMER, ENVELOPE, ROOT, ROOT_MSG, entry, msg, payload,
)
from tools.action_item_kinds import SchemaError, build_envelope
from tools.action_item_kinds import slack as slack_mod
from tools.action_item_kinds.slack import SlackSources
from tools.action_item_kinds.spec import SourceUnavailable

ROOT_TOKEN = "xoxp-root-shared-reader-0001"
OWN_TOKEN = "xoxp-profile-own-0002"
HOSTS = {"github.com", "linear.app", "mail.google.com", "app.slack.com"}


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """A named profile home under a temp Hermes root; neither has a token until a test writes one."""
    root = tmp_path / ".hermes"
    home = root / "profiles" / "crew-planner"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("{}\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    return root, home


def write_root_token(root: Path, token: str = ROOT_TOKEN) -> None:
    (root / ".env").write_text(f"OTHER=1\nexport SLACK_USER_TOKEN=\"{token}\"  # shared reader\n")


class FakeSlackHttp:
    """Stands in for ``urlopen``: records each bearer and answers from fixed thread/channel lists."""

    def __init__(self, thread=(), channel=(), *, error: str | None = None, status: int | None = None):
        self.thread, self.channel, self.error, self.status = list(thread), list(channel), error, status
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, request, timeout=None):
        url = urllib.parse.urlparse(request.full_url)
        method = url.path.rsplit("/", 1)[-1]
        self.calls.append((request.get_method(), method, request.get_header("Authorization")))
        if self.status is not None:
            raise urllib.error.HTTPError(request.full_url, self.status, "Forbidden", Message(), io.BytesIO(b""))
        if self.error:
            body = {"ok": False, "error": self.error}
        else:
            oldest = float(urllib.parse.parse_qs(url.query)["oldest"][0])
            pool = self.thread if method == "conversations.replies" else self.channel
            body = {"ok": True, "messages": [m for m in pool if float(m["ts"]) >= oldest]}
        return io.BytesIO(json.dumps(body).encode("utf-8"))


def install(monkeypatch, fake: FakeSlackHttp) -> FakeSlackHttp:
    monkeypatch.setattr(slack_mod.urllib.request, "urlopen", fake)
    return fake


def read(sources: SlackSources | None = None) -> dict:
    return (sources or SlackSources()).slack_conversation(CHANNEL, f"{ROOT:.6f}", ROOT - 1)


def all_text(exc: BaseException) -> str:
    parts, seen = [], exc
    while seen is not None:
        parts += [str(seen), repr(seen), *getattr(seen, "__notes__", [])]
        seen = seen.__cause__ or seen.__context__
    return "\n".join(parts)


def test_a_profile_without_a_token_reads_with_the_shared_root_token(profile, monkeypatch):
    root, home = profile
    write_root_token(root)
    fake = install(monkeypatch, FakeSlackHttp(thread=[ROOT_MSG], channel=[ROOT_MSG]))
    assert read() == {"thread": [ROOT_MSG], "channel": [ROOT_MSG]}
    assert fake.calls == [("GET", "conversations.replies", f"Bearer {ROOT_TOKEN}"),
                          ("GET", "conversations.history", f"Bearer {ROOT_TOKEN}")]
    # Read in-process only: nothing copied into the profile or the environment.
    assert not (home / ".env").exists() and "SLACK_USER_TOKEN" not in os.environ


def test_the_profiles_own_token_wins_over_the_shared_one(profile, monkeypatch):
    root, _ = profile
    write_root_token(root)
    monkeypatch.setenv("SLACK_USER_TOKEN", OWN_TOKEN)
    fake = install(monkeypatch, FakeSlackHttp(thread=[ROOT_MSG], channel=[ROOT_MSG]))
    read()
    assert {bearer for _, _, bearer in fake.calls} == {f"Bearer {OWN_TOKEN}"}


@pytest.mark.parametrize("fake", [FakeSlackHttp(error="not_in_channel"), FakeSlackHttp(error="channel_not_found"),
                                  FakeSlackHttp(error="missing_scope"), FakeSlackHttp(status=403)],
                         ids=["not_in_channel", "channel_not_found", "missing_scope", "http_403"])
def test_an_unreadable_conversation_still_refuses_and_never_shows_the_token(profile, monkeypatch, fake):
    root, _ = profile
    write_root_token(root)
    install(monkeypatch, fake)
    with pytest.raises(SourceUnavailable) as err:
        read()
    assert ROOT_TOKEN not in all_text(err.value)
    with pytest.raises(SchemaError, match="cannot verify the Slack conversation is current") as refused:
        build_envelope("send_message", payload=payload([entry(ROOT, "Can we move the pickup to Thursday?")]),
                       sources=SlackSources(), hosts=HOSTS, **ENVELOPE)
    assert ROOT_TOKEN not in all_text(refused.value)


def test_no_token_in_the_profile_or_the_root_refuses(profile, monkeypatch):
    fake = install(monkeypatch, FakeSlackHttp())
    with pytest.raises(SourceUnavailable, match="neither this profile nor the shared Slack reader has SLACK_USER_TOKEN"):
        read()
    assert fake.calls == []


def test_a_routed_profile_never_falls_back_to_the_shared_reader(profile, monkeypatch):
    from agent.secret_scope import (
        UnscopedSecretError, reset_multiplex_context, reset_secret_scope, set_multiplex_context, set_secret_scope,
    )

    root, home = profile
    write_root_token(root)
    fake = install(monkeypatch, FakeSlackHttp(thread=[ROOT_MSG], channel=[ROOT_MSG]))
    # A scope bound for another home (dashboard/desktop backend serving this profile).
    scope = set_secret_scope({}, profile_home=str(root / "profiles" / "other"))
    try:
        with pytest.raises(SourceUnavailable, match="a routed profile does not use the shared Slack reader"):
            read()
    finally:
        reset_secret_scope(scope)
    # Multiplexing gateway with this profile's own (tokenless) scope.
    mux = set_multiplex_context(True)
    try:
        scope = set_secret_scope({}, profile_home=str(home))
        try:
            with pytest.raises(SourceUnavailable, match="a routed profile"):
                read()
        finally:
            reset_secret_scope(scope)
        # Multiplex with no scope at all fails closed before any read, as it always did.
        with pytest.raises(UnscopedSecretError):
            read()
    finally:
        reset_multiplex_context(mux)
    assert fake.calls == []


def test_the_tool_parks_a_current_slack_reply_through_the_shared_reader(profile, monkeypatch):
    """End to end: ``action_item_add`` with the host's real sources, only the HTTP layer faked."""
    from tests.tools.test_action_item_tools import as_worker, call, card_state, make_card
    from hermes_cli import kanban_db as kb
    from tools.action_item_kinds.spec import LiveSources
    from tools.registry import invalidate_check_fn_cache

    root, _ = profile
    write_root_token(root)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_DB", "HERMES_PROFILE_NAME", "HERMES_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    invalidate_check_fn_cache()

    class NoGh(LiveSources):
        pass

    monkeypatch.setattr("tools.action_item_kinds.github.GhSources", NoGh)
    late = ROOT + 300.0
    fake = install(monkeypatch, FakeSlackHttp(thread=[ROOT_MSG], channel=[ROOT_MSG, msg(late, CUSTOMER, "Hello?")]))
    tid, run_id = make_card("crew-planner", claim=True)
    as_worker(monkeypatch, "crew-planner", tid, run_id)

    stale = payload([entry(ROOT, "Can we move the pickup to Thursday?")])
    out = call("action_item_add", kind="send_message", payload=stale, **ENVELOPE)
    assert "payload.last_messages: the Slack conversation has 1 message(s)" in out["error"], out
    fresh = payload([entry(ROOT, "Can we move the pickup to Thursday?"), entry(late, "Hello?")])
    out = call("action_item_add", kind="send_message", payload=fresh, **ENVELOPE)
    assert out["ok"], out
    assert {bearer for _, _, bearer in fake.calls} == {f"Bearer {ROOT_TOKEN}"}
    _, comments, events = card_state(tid)
    stored = json.dumps([c.body for c in comments]) + json.dumps([e.payload for e in events], default=str)
    assert ROOT_TOKEN not in stored and ROOT_TOKEN not in json.dumps(out)
