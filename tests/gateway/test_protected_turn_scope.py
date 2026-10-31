"""Review regressions with real owner/card/child and NO-REAL-PROVIDER fixtures."""
import asyncio
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.gateway.test_protected_conversation import native_claim


def test_mock_agent_does_not_opt_into_protected_entry_or_executor():
    from agent.protected_conversation import protected_turn_entry
    from agent.tool_executor import execute_tool_calls_concurrent
    @protected_turn_entry
    def ordinary(agent, user_message, history=None):
        return 'ORDINARY'
    assert ordinary(MagicMock(), 'fixture', history=[]) == 'ORDINARY'
    with pytest.raises(AttributeError):
        execute_tool_calls_concurrent(MagicMock(), None, [], '')


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('mode', ['success', 'replay', 'stop', 'card', 'cancel', 'rotation', 'after-tool'])
def test_owner_scope_retires_and_reaps_on_all_exits(tmp_path, monkeypatch, mode):
    from gateway.session_protected_scope import protected_turn_scope
    from gateway.session_protected_tools import SentinelLaunch
    from agent.transports.types import NormalizedResponse, ToolCall
    from hermes_state_runtime import RuntimeStoreError, get_session_admission
    from run_agent import AIAgent
    authority, context, admission = native_claim(tmp_path, monkeypatch)
    (tmp_path / 'allowed.txt').write_text('NONSECRET\n')
    requests = []
    async def exercise():
        ready = asyncio.Event()
        captured = []
        def controlled(messages, tools):
            requests.append(messages)
            if mode == 'stop':
                captured[0].proxy.worker.stop.set()
            if mode == 'rotation':
                from hermes_state_runtime import begin_runtime_epoch
                begin_runtime_epoch(authority.db, instance_id='rotated-owner')
            if mode == 'card':
                from hermes_cli import kanban_db as kb
                from hermes_cli.kanban_db_connect import connect
                with closing(connect(board='controlled')) as conn:
                    kb.block_task(conn, context['task_id'], reason='fixture stop', expected_run_id=context['run_id'])
            if mode == 'success':
                return NormalizedResponse('CONTROLLED', None, 'stop')
            return NormalizedResponse(None, [ToolCall('one-id', 'read_file', '{}')], 'tool_calls')
        async def owned():
            async with protected_turn_scope(authority, SimpleNamespace(session_id='owned'), admission,
                    SentinelLaunch(tmp_path.resolve()), principal_id='fixture-owner', context=context, complete=controlled) as turn:
                captured.append(turn)
                if mode == 'after-tool':
                    actual = turn.proxy.invoke
                    def revoke_after_tool(name, args):
                        result = actual(name, args)
                        turn.proxy.worker.stop.set()
                        return result
                    monkeypatch.setattr(turn.proxy, 'invoke', revoke_after_tool)
                ready.set()
                if mode == 'cancel':
                    await asyncio.Event().wait()
                agent = object.__new__(AIAgent)
                agent._protected_turn = turn
                return await asyncio.to_thread(agent.run_conversation, 'CONTROLLED_SCOPE')
        try:
            if mode == 'cancel':
                task = asyncio.create_task(owned())
                await ready.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif mode == 'success':
                assert (await owned())['completed']
            else:
                with pytest.raises((RuntimeError, RuntimeStoreError, ValueError)):
                    await owned()
            proxy = captured[0].proxy
            assert proxy.worker.process.poll() is not None
            assert authority._managed_workers == {} and authority._managed_spawning == {}
            current = get_session_admission(authority.db, admission_id=admission['admission_id'])
            if mode != 'rotation':
                assert current['status'] == 'unknown'
                with authority.db._read_ctx() as conn:
                    assert conn.execute('SELECT status FROM worker_executions').fetchone()[0] == 'terminal'
            else:
                assert current['status'] == 'started' and current['owner_epoch'] == authority.epoch
                assert authority.db._read_one('SELECT epoch FROM runtime_epoch')[0] != authority.epoch
                assert authority.db._read_one('SELECT status FROM worker_executions')[0] == 'running'
            if mode == 'replay':
                assert proxy.sequence == 1
                assert len(authority.db.get_messages('owned')) == 3
            if mode == 'after-tool':
                assert proxy.sequence == 1
                assert len(authority.db.get_messages('owned')) == 2  # no stale result published
        finally:
            authority.db.close()
    asyncio.run(exercise())


@pytest.mark.platforms('posix')
def test_owner_close_can_retry_a_storage_failure(tmp_path, monkeypatch):
    from gateway.session_protected_capabilities import NativeLifecycle
    from gateway.session_managed_worker import _spawn_registered_worker
    from gateway.session_protected_tools import SentinelLaunch, ToolProxy
    from gateway import session_worker_reservation as reservation
    authority, context, admission = native_claim(tmp_path, monkeypatch)
    async def exercise():
        worker, process = await _spawn_registered_worker(authority, SimpleNamespace(session_id='owned'), admission,
            sentinel=SentinelLaunch(tmp_path.resolve()))
        try:
            ToolProxy(worker, SentinelLaunch(tmp_path.resolve())).bind()
            owner = NativeLifecycle.reserve(authority, admission_id=admission['admission_id'],
                principal_id='fixture-owner', process=process, context=context)
            actual = reservation.lose_admission_worker
            monkeypatch.setattr(reservation, 'lose_admission_worker', lambda *a, **k: (_ for _ in ()).throw(OSError('controlled I/O')))
            with pytest.raises(OSError):
                owner.close()
            monkeypatch.setattr(reservation, 'lose_admission_worker', actual)
            owner.close()
            with authority.db._read_ctx() as conn:
                assert conn.execute('SELECT status FROM worker_executions').fetchone()[0] == 'terminal'
        finally:
            worker.close()
            authority.db.close()
    asyncio.run(exercise())
