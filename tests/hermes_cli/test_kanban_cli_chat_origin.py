"""`hermes kanban create` run from a chat session subscribes that chat (chat-origin rows).

Observed failure (Nov 25, 2026): the Captain session in a Telegram topic created a card with
the terminal command ``hermes kanban create``. Only the ``kanban_create`` tool auto-subscribed
the calling chat, so the card and the cards its worker created had no subscription to that
topic, and their completions never reached it.
"""
from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn

# The env a gateway terminal child gets from the session context (tools/environments/local.py
# ``_inject_session_context_env``): a Captain in Telegram forum topic 54436.
TOPIC_ENV = {
    "HERMES_SESSION_PLATFORM": "telegram",
    "HERMES_SESSION_CHAT_ID": "8729204498",
    "HERMES_SESSION_THREAD_ID": "54436",
    "HERMES_SESSION_CHAT_TYPE": "thread",
    "HERMES_SESSION_USER_ID": "jack",
    "HERMES_SESSION_PROFILE": "main-captain",
}
SESSION_VARS = (*TOPIC_ENV, "HERMES_SESSION_KEY", "HERMES_SESSION_ID", "HERMES_SESSION_MESSAGE_ID",
                "HERMES_SESSION_SCOPE_ID", "HERMES_SESSION_PARENT_CHAT_ID", "HERMES_SESSION_USER_ID_ALT")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    for name in SESSION_VARS:
        monkeypatch.delenv(name, raising=False)
    kb.init_db()
    return home


def _kanban(*argv: str) -> int:
    parser = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(parser.add_subparsers(dest="command"))
    return kc.kanban_command(parser.parse_args(["kanban", *argv]))


def _create_json(capsys, *argv: str) -> str:
    assert _kanban("create", *argv, "--json") == 0
    return json.loads(capsys.readouterr().out)["id"]


def _subs(task_id: str) -> list[dict]:
    with kbc.connect_closing() as conn:
        return kbn.list_notify_subs(conn, task_id)


def test_cli_create_from_chat_session_subscribes_that_topic(kanban_home, monkeypatch, capsys):
    for name, value in TOPIC_ENV.items():
        monkeypatch.setenv(name, value)
    assert _kanban("create", "mockup round 5", "--assignee", "builder") == 0
    # Stdout stays the one line callers parse (`... | tail -1`, kanban_v2_workspace.py).
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith("Created t_")
    task_id = out[0].split()[1]

    subs = _subs(task_id)
    assert len(subs) == 1
    sub = subs[0]
    assert (sub["platform"], sub["chat_id"], sub["thread_id"], sub["chat_type"]) == (
        "telegram", "8729204498", "54436", "thread")
    assert sub["notifier_profile"] == "main-captain"
    assert sub["delivery_mode"] == "notify+wake"
    assert sub["delivery_metadata"][kbn.CHAT_ORIGIN_KEY] is True


def test_cli_create_without_chat_session_writes_no_subscription(kanban_home, capsys):
    task_id = _create_json(capsys, "cron card", "--assignee", "builder")
    assert _subs(task_id) == []


def test_worker_child_inherits_chat_origin_row(kanban_home, monkeypatch, capsys):
    """A card's worker has no chat in its env: its children get the creator card's rows."""
    for name, value in TOPIC_ENV.items():
        monkeypatch.setenv(name, value)
    owner = _create_json(capsys, "Jack review: round 4", "--assignee", "main-captain")
    for name in TOPIC_ENV:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HERMES_KANBAN_TASK", owner)
    child = _create_json(capsys, "mockup round 5", "--assignee", "builder")

    subs = _subs(child)
    assert len(subs) == 1
    assert (subs[0]["thread_id"], subs[0]["delivery_mode"]) == ("54436", "notify+wake")
    assert subs[0]["delivery_metadata"][kbn.CHAT_ORIGIN_KEY] is True


def test_notify_subscribe_chat_origin_flag_survives_a_later_mode_change(kanban_home, capsys):
    """Backfill marks a row by hand; a later re-subscribe without the flag (owner watch turning
    the route wake-only) changes the mode and keeps the mark."""
    task_id = _create_json(capsys, "old card", "--assignee", "builder")
    target = ("--platform", "telegram", "--chat-id", "8729204498", "--thread-id", "54436",
              "--chat-type", "thread", "--notifier-profile", "main-captain")
    assert _kanban("notify-subscribe", task_id, *target, "--delivery-mode", "notify+wake",
                   "--chat-origin") == 0
    assert _kanban("notify-subscribe", task_id, *target, "--delivery-mode", "wake") == 0

    (sub,) = _subs(task_id)
    assert sub["delivery_mode"] == "wake"
    assert sub["delivery_metadata"] == {kbn.CHAT_ORIGIN_KEY: True}


def test_notify_subscribe_without_flag_writes_no_chat_origin(kanban_home, capsys):
    task_id = _create_json(capsys, "automation card", "--assignee", "builder")
    assert _kanban("notify-subscribe", task_id, "--platform", "telegram", "--chat-id", "c",
                   "--delivery-mode", "wake") == 0
    (sub,) = _subs(task_id)
    assert kbn.CHAT_ORIGIN_KEY not in (sub["delivery_metadata"] or {})
