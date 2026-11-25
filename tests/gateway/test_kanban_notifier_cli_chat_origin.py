"""A card created with `hermes kanban create` from a Telegram topic is announced in that topic.

End to end through the notifier: create from the chat session's terminal env, a worker
completes the card, and one notifier tick sends the passive ping to the topic AND wakes the
creating session there (the ``notify+wake`` the auto-subscription records). Before the CLI
auto-subscribed, the card had no subscription and the tick delivered nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import json

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

TOPIC_ENV = {
    "HERMES_SESSION_PLATFORM": "telegram",
    "HERMES_SESSION_CHAT_ID": "8729204498",
    "HERMES_SESSION_THREAD_ID": "54436",
    "HERMES_SESSION_CHAT_TYPE": "thread",
    "HERMES_SESSION_USER_ID": "jack",
}


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


async def _one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def test_cli_created_topic_card_pings_and_wakes_its_topic_on_completion(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    for name, value in TOPIC_ENV.items():
        monkeypatch.setenv(name, value)
    parser = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(parser.add_subparsers(dest="command"))
    assert kc.kanban_command(parser.parse_args(
        ["kanban", "create", "Mobile mockup round 6", "--assignee", "builder", "--json"])) == 0
    task_id = json.loads(capsys.readouterr().out)["id"]
    for name in TOPIC_ENV:
        monkeypatch.delenv(name)

    with kbc.connect_closing() as conn:
        kb.complete_task(conn, task_id, summary="Round 6 screenshots pushed; URLs on the card.")

    adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    asyncio.run(_one_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "8729204498"
    assert adapter.sent[0]["metadata"]["thread_id"] == "54436"
    assert task_id in adapter.sent[0]["text"]
    assert len(adapter.handled) == 1
    woken = adapter.handled[0]
    assert (woken.source.chat_id, woken.source.thread_id, woken.source.chat_type) == (
        "8729204498", "54436", "thread")
    assert task_id in woken.text
