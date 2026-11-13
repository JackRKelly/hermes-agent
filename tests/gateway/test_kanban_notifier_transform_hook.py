"""``transform_kanban_notification``: plugins and shell hooks own the text of a Kanban ping.

Drives the real notifier tick (claim, format, send, cursor) against a real board DB and the real
per-home plugin manager. Callbacks are registered the way ``ctx.register_hook`` stores them, and
shell hooks through ``agent.shell_hooks.register_from_config`` exactly as gateway startup does.
"""

import asyncio

import pytest

from agent import shell_hooks
from gateway.config import GatewayConfig, Platform
from gateway.kanban_watchers_notifier import _KanbanNotification
from gateway.run import GatewayRunner, _profile_runtime_scope
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn

HOOK = "transform_kanban_notification"
LONG_SUMMARY = ("Opened owner/repo#12 with the fix. " * 40).strip()  # ~1400 chars, past the payload clip
PARKED = {"reason": "[needs-jack:decision] Which fix should the crew build? then=ready", "kind": "needs_input"}
BLOCKED = {"reason": "needs a reviewer profile", "kind": "needs_input"}
KINDS = ("completed", "blocked", "block_loop_detected")


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append(text)

    async def handle_message(self, event):
        event._gateway_accepted = True


@pytest.fixture(autouse=True)
def _clean_hooks():
    from hermes_cli.plugins import _reset_plugin_managers_for_tests
    shell_hooks.reset_for_tests()
    _reset_plugin_managers_for_tests()
    yield
    shell_hooks.reset_for_tests()
    _reset_plugin_managers_for_tests()


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    kb.init_db()


@pytest.fixture
def artifact_calls(monkeypatch):
    calls = []

    async def spy(self, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(GatewayRunner, "_deliver_kanban_artifacts", spy)
    return calls


def _manager():
    from hermes_cli.plugins import get_plugin_manager
    manager = get_plugin_manager()
    manager._discovered = True  # no on-disk plugins in the test home; skip discovery
    return manager


def _register(callback):
    """Register *callback* on the ACTIVE home's plugin manager (what a loaded plugin does)."""
    _manager()._hooks.setdefault(HOOK, []).append(callback)


def _shell_hook(tmp_path, name, stdout):
    """Register a real shell hook printing *stdout* from the ACTIVE home's ``hooks:`` config."""
    script = tmp_path / name
    script.write_text(f"#!/bin/sh\ncat > /dev/null\nprintf '%s' '{stdout}'\n")
    script.chmod(0o755)
    _manager()
    assert shell_hooks.register_from_config({"hooks": {HOOK: [{"command": str(script)}]}}, accept_hooks=True)


def _card(*events):
    """A card with one telegram subscription and the given ``(kind, payload)`` events."""
    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="receiving live status", assignee="engineering")
        kbn.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1", delivery_mode="notify")
        for kind, payload in events:
            kb._append_event(conn, task_id, kind=kind, payload=payload)
        return task_id
    finally:
        conn.close()


def _completed_card():
    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="receiving live status", assignee="engineering")
        kbn.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1", delivery_mode="notify")
        assert kb.complete_task(conn, task_id, summary=LONG_SUMMARY)
        return task_id
    finally:
        conn.close()


def _sub(task_id):
    conn = kbc.connect()
    try:
        return next(s for s in kbn.list_notify_subs(conn) if s["task_id"] == task_id)
    finally:
        conn.close()


def _unseen(task_id):
    conn = kbc.connect()
    try:
        _, events = kbn.unseen_events_for_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1",
                                              thread_id="", kinds=KINDS)
        return events
    finally:
        conn.close()


def _tick(monkeypatch, adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(runner._kanban_notifier_watcher(interval=1))


def _blocked_text(task_id):
    return f"⏸ [default] @engineering Kanban {task_id} blocked: needs a reviewer profile"


def test_no_hook_sends_the_core_text_byte_for_byte(board, monkeypatch):
    task_id = _card(("blocked", BLOCKED))
    adapter = RecordingAdapter()

    _tick(monkeypatch, adapter)

    assert adapter.sent == [_blocked_text(task_id)]


def test_python_hook_replaces_the_text_and_gets_the_payload(board, monkeypatch):
    seen = []

    def hook(**kwargs):
        seen.append(kwargs)
        return f"🟡 {kwargs['task_title']} is blocked"

    _register(hook)
    task_id = _card(("blocked", BLOCKED))
    adapter = RecordingAdapter()

    _tick(monkeypatch, adapter)

    assert adapter.sent == ["🟡 receiving live status is blocked"]
    (kwargs,) = seen
    assert kwargs["default_text"] == _blocked_text(task_id)
    assert {k: kwargs[k] for k in ("event_kind", "task_id", "board", "task_assignee", "task_status", "platform",
                                   "chat_id", "thread_id", "notifier_profile", "delivery_mode")} == {
        "event_kind": "blocked", "task_id": task_id, "board": "default", "task_assignee": "engineering",
        "task_status": "ready", "platform": "telegram", "chat_id": "chat-1", "thread_id": None,
        "notifier_profile": None, "delivery_mode": "notify"}
    assert kwargs["event_payload"] == BLOCKED
    assert _unseen(task_id) == []


def test_run_summary_is_the_full_run_handoff(board, monkeypatch, artifact_calls):
    seen = []
    _register(lambda **kwargs: seen.append(kwargs))
    task_id = _completed_card()
    adapter = RecordingAdapter()

    _tick(monkeypatch, adapter)

    (kwargs,) = seen
    assert kwargs["event_kind"] == "completed"
    assert kwargs["run_summary"] == LONG_SUMMARY
    assert len(kwargs["event_payload"]["summary"]) < len(LONG_SUMMARY)  # the payload copy is clipped
    # A None return keeps the core rendering, artifacts included.
    assert len(adapter.sent) == 1 and adapter.sent[0].startswith(f"✔ [default] @engineering Kanban {task_id} done")
    assert len(artifact_calls) == 1


def test_silence_sends_nothing_uploads_nothing_and_never_resends(board, monkeypatch, artifact_calls):
    _register(lambda **kwargs: "")
    task_id = _completed_card()
    adapter = RecordingAdapter()

    _tick(monkeypatch, adapter)

    assert adapter.sent == []
    assert artifact_calls == []
    assert _unseen(task_id) == []
    assert _sub(task_id)["last_ping_event_id"] > 0  # recorded as delivered
    _tick(monkeypatch, adapter)
    assert adapter.sent == [] and artifact_calls == []


def test_raising_hook_fails_open_and_the_next_valid_result_wins(board, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("formatter bug")

    _register(broken)
    _register(lambda **kwargs: {"bogus": 1})  # invalid: skipped
    _register(lambda **kwargs: {"text": "second hook"})
    _register(lambda **kwargs: "never used")
    _card(("blocked", BLOCKED))
    adapter = RecordingAdapter()

    _tick(monkeypatch, adapter)

    assert adapter.sent == ["second hook"]


def test_raising_hook_alone_keeps_the_core_text(board, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("formatter bug")

    _register(broken)
    task_id = _card(("blocked", BLOCKED))
    adapter = RecordingAdapter()

    _tick(monkeypatch, adapter)

    assert adapter.sent == [_blocked_text(task_id)]
    assert _unseen(task_id) == []


def test_jack_park_is_withheld_before_the_hook_runs(board, monkeypatch):
    seen = []

    def hook(**kwargs):
        seen.append(kwargs["event_kind"])
        return "rewritten"

    _register(hook)
    task_id = _card(("blocked", PARKED), ("blocked", BLOCKED))
    adapter = RecordingAdapter()

    _tick(monkeypatch, adapter)

    assert seen == ["blocked"]  # only the non-park block reached the hook
    assert adapter.sent == ["rewritten"]
    assert _unseen(task_id) == []


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("stdout, expected", [
    ('{"text": "📦 card blocked"}', "📦 card blocked"),
    ("not json", None),
    ('{"text": 5}', None),
    ("", None),
])
def test_shell_hook_text_and_fail_open(board, monkeypatch, tmp_path, stdout, expected):
    _shell_hook(tmp_path, "fmt.sh", stdout)
    task_id = _card(("blocked", BLOCKED))
    adapter = RecordingAdapter()

    _tick(monkeypatch, adapter)

    assert adapter.sent == [expected or _blocked_text(task_id)]


@pytest.mark.platforms("posix")
def test_shell_hook_silent(board, monkeypatch, tmp_path):
    _shell_hook(tmp_path, "quiet.sh", '{"silent": true}')
    task_id = _card(("blocked", BLOCKED))
    adapter = RecordingAdapter()

    _tick(monkeypatch, adapter)

    assert adapter.sent == []
    assert _unseen(task_id) == []
    assert _sub(task_id)["last_ping_event_id"] > 0


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("multiplex, notifier_profile, expected", [
    (True, "engineering", "from engineering"),  # multiplexed: the subscription's profile's hooks:
    (False, "engineering", "from launch"),     # standalone gateway: its own (launch) profile's hooks:
    (True, None, "from launch"),               # unstamped subscription: the launch profile's hooks:
])
def test_hooks_come_from_the_subscription_profile_under_multiplex(
        board, tmp_path, multiplex, notifier_profile, expected):
    """Shell hooks are per profile home, not process-global: the ping is transformed inside
    ``_owner_scope()``, and the hook call carries that scope to its worker thread."""
    eng_home = tmp_path / "profiles" / "engineering"
    eng_home.mkdir(parents=True)
    _shell_hook(tmp_path, "launch.sh", '{"text": "from launch"}')
    with _profile_runtime_scope(eng_home):
        _shell_hook(tmp_path, "eng.sh", '{"text": "from engineering"}')

    task_id = _card(("blocked", BLOCKED))
    sub = {**_sub(task_id), "notifier_profile": notifier_profile}
    conn = kbc.connect()
    try:
        _, events = kbn.unseen_events_for_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1",
                                              thread_id="", kinds=KINDS)
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=multiplex)
    runner._resolve_profile_home_for_source = lambda source: eng_home
    runner._kanban_sub_op = lambda *args, **kwargs: None
    adapter = RecordingAdapter()
    n = _KanbanNotification(runner, {"sub": sub, "task": task, "events": events, "board": "default",
                                     "cursor": events[-1].id, "old_cursor": 0, "run_summaries": {}},
                            platform_cls=Platform, sub_fail_counts={})
    n.plat, n.adapter = Platform.TELEGRAM, adapter

    async def deliver():
        async with n._owner_scope():
            assert await n._send_pings()

    asyncio.run(deliver())

    assert adapter.sent == [expected]
