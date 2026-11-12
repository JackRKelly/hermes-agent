"""A block parked for Jack reaches him through the Action Items queue only.

The notifier must neither ping nor wake a subscribed topic for it (the ping would post the
question itself), yet still settle the claim so it is never re-sent. Other blocks keep notifying.
"""
import asyncio
from types import SimpleNamespace

import pytest

import gateway.wake
from gateway.config import Platform
from gateway.kanban_watchers_notifier import jack_park_event
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn

# Payload of the live event that posted a decision question into the Captain topic.
PARKED_PAYLOAD = {
    "reason": "[needs-jack:decision] Which fix should the crew build for the receiving live status gap? "
              "then=ready item=228338e505002360",
    "kind": "needs_input", "recurrences": 2, "source_status": "ready", "limit": 2, "actor": "engineering",
}
_KINDS = ["blocked", "block_loop_detected"]


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


async def _run_one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _card_with_event(kind, payload, *more):
    """A card with a notify+wake topic sub and the events ``(kind, payload), *more`` in order."""
    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="receiving live status", assignee="engineering",
                                 session_id="agent:main:telegram:thread:chat-1:topic-7")
        kbn.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1", thread_id="topic-7",
                           chat_type="thread", delivery_mode="notify+wake",
                           delivery_metadata={"thread_id": "topic-7", "chat_type": "thread"})
        for ev_kind, ev_payload in ((kind, payload), *more):
            kb._append_event(conn, task_id, kind=ev_kind, payload=ev_payload)
        return task_id
    finally:
        conn.close()


def _unseen(task_id):
    conn = kbc.connect()
    try:
        _, events = kbn.unseen_events_for_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1",
                                              thread_id="topic-7", kinds=_KINDS)
        return events
    finally:
        conn.close()


@pytest.fixture
def wake_spy(monkeypatch):
    calls = []
    real = gateway.wake.deliver_wake

    async def spy(*args, **kwargs):
        calls.append(kwargs.get("text"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(gateway.wake, "deliver_wake", spy)
    return calls


def test_jack_park_block_is_neither_pinged_nor_woken_and_settles(tmp_path, monkeypatch, wake_spy):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "park.db"))
    kb.init_db()
    task_id = _card_with_event("block_loop_detected", PARKED_PAYLOAD)
    adapter = RecordingAdapter()

    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))

    assert adapter.sent == []
    assert adapter.handled == []
    assert wake_spy == []
    assert _unseen(task_id) == []

    # The settled claim is never replayed by a later tick.
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))
    assert adapter.sent == [] and adapter.handled == [] and wake_spy == []


def test_routing_block_still_pings_and_wakes(tmp_path, monkeypatch, wake_spy):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "routing.db"))
    kb.init_db()
    task_id = _card_with_event("blocked", {"reason": "[captain:routing] no reviewer profile for this repo",
                                           "kind": "needs_input"})
    adapter = RecordingAdapter()

    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))

    assert len(adapter.sent) == 1
    assert "[captain:routing] no reviewer profile" in adapter.sent[0]["text"]
    assert len(adapter.handled) == 1
    assert len(wake_spy) == 1
    assert _unseen(task_id) == []


def test_park_then_later_untagged_block_pings_only_the_later_block(tmp_path, monkeypatch, wake_spy):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "mixed.db"))
    kb.init_db()
    task_id = _card_with_event("block_loop_detected", PARKED_PAYLOAD,
                               ("blocked", {"reason": "worker crashed twice on the receiving migration",
                                            "kind": "needs_input"}))
    adapter = RecordingAdapter()

    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))

    assert len(adapter.sent) == 1
    assert "worker crashed twice" in adapter.sent[0]["text"]
    assert "Which fix should the crew build" not in adapter.sent[0]["text"]
    assert len(wake_spy) == 1 and "Which fix should the crew build" not in (wake_spy[0] or "")
    assert _unseen(task_id) == []


@pytest.mark.parametrize("kind,reason,parked", [
    ("blocked", "[needs-jack] pick one", True),
    ("blocked", "[Needs-Jack:approval] ok?", True),
    ("blocked", "[decision] Consult Jack: which plan", True),
    ("blocked", "[decision:scope] option A or B?", True),
    ("block_loop_detected", "[awaiting-approval:merge] merge #5 sha=abc", True),
    ("blocked", "[captain:routing] re-parked; Jack already answered the [needs-jack:decision] item", False),
    ("blocked", "[hold:jack] waiting on vendor, see [decision] note", False),
    ("blocked", "push failed after posting the [action-item:v1] comment", False),
    ("blocked", "worker needs input: which branch?", False),
    ("blocked", "[needs-jackson] unrelated tag", False),
    ("changes_requested", "[needs-jack] review", False),
    ("completed", None, False),
])
def test_jack_park_classification(kind, reason, parked):
    ev = SimpleNamespace(kind=kind, payload={"reason": reason, "kind": "needs_input"})
    assert jack_park_event(ev) is parked
