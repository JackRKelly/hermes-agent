"""Controlled NO-REAL-PROVIDER contracts; not protected identity/admission proof."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.platforms('posix')
def test_actual_conversation_entry_and_aiagent_dispatch_use_only_native_proxy(tmp_path, monkeypatch):
    from agent.transports.types import NormalizedResponse, ToolCall
    from gateway.session_managed_worker import _spawn_registered_worker
    from gateway.session_protected_tools import SentinelLaunch, ToolProxy
    from gateway.session_protected_capabilities import NativeLifecycle, ScopedInference
    from agent.protected_conversation import ProtectedTurn

    from run_agent import AIAgent

    (tmp_path / 'allowed.txt').write_text('CONTROLLED_NO_PROVIDER\n')
    authority, context, admission = native_claim(tmp_path, monkeypatch)
    async def exercise():
        worker, process = await _spawn_registered_worker(authority, SimpleNamespace(session_id='owned'),
            {'generation': admission['generation']}, sentinel=SentinelLaunch(tmp_path.resolve()))
        proxy = ToolProxy(worker, SentinelLaunch(tmp_path.resolve()))
        lifecycle = None
        try:
            await asyncio.to_thread(proxy.bind)
            lifecycle = await asyncio.to_thread(NativeLifecycle.reserve, authority, admission_id=admission['admission_id'],
                principal_id='fixture-owner', process=process, context=context)
            requests = []
            def controlled_inference(messages, tools):
                requests.append((messages, tools))
                if len(requests) == 1:
                    return NormalizedResponse(None, [ToolCall('read-1', 'read_file', '{}')], 'tool_calls')
                assert messages[-1]['content'] == '1|CONTROLLED_NO_PROVIDER'
                return NormalizedResponse('CONTROLLED_DONE', None, 'stop')
            inference = ScopedInference(controlled_inference, lifecycle.binding)
            agent = object.__new__(AIAgent)  # no ordinary constructor, provider, hooks or config
            agent._protected_turn = ProtectedTurn(inference.client(), lifecycle.client(), proxy)
            # If ordinary dispatch/inline routes are reached, this fixture fails immediately.
            agent._execute_tool_calls_sequential = lambda *a, **k: pytest.fail('host dispatch reached')
            for target in ('agent.background_review.cancel_background_review_for_live_turn',
                    'agent.turn_finalizer.settle_held_final', 'agent.turn_context.export_current_turn_boundary',
                    'tools.vision_tools_history_budget.native_turn_images'):
                monkeypatch.setattr(target, lambda *a, **k: pytest.fail('ordinary pre/post phase reached'))
            result = await asyncio.to_thread(agent.run_conversation, 'Read the nonsecret sentinel')
            assert result['final_response'] == 'CONTROLLED_DONE'
            assert result['api_calls'] == 2 and result['completed']
            assert len(requests) == 2 and requests[0][1] == requests[1][1]
            assert authority.db.get_messages('owned')[-1]['content'] == 'CONTROLLED_DONE'
            with authority.db._read_ctx() as conn:
                assert conn.execute('SELECT status FROM worker_executions').fetchone()[0] == 'running'
            # Running a turn must NOT release/complete a card or settle its admission.
            from hermes_cli.kanban_db_connect import connect
            from hermes_cli import kanban_db as kb
            with closing(connect(board='controlled')) as conn:
                assert kb.get_task(conn, context['task_id']).status == 'running'
            from hermes_state_runtime import get_session_admission
            assert get_session_admission(authority.db, admission_id=admission['admission_id'])['status'] == 'started'
            await asyncio.to_thread(lifecycle.close)
            assert get_session_admission(authority.db, admission_id=admission['admission_id'])['status'] == 'unknown'
            with authority.db._read_ctx() as conn:
                assert conn.execute('SELECT status FROM worker_executions').fetchone()[0] == 'terminal'
            with pytest.raises(Exception, match='protected_capability_denied'):
                lifecycle.client().request('check', {})
        finally:
            if lifecycle is not None:
                await asyncio.to_thread(lifecycle.close)
            worker.close()
            authority.db.close()
    asyncio.run(exercise())


def native_claim(tmp_path, monkeypatch, *, persisted=False):
    """Real isolated card claim, policy and native reservation store (not a daemon)."""
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from gateway.session_contract import Principal
    from gateway.session_kanban import build_kanban_policy
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda name: str(tmp_path))
    kb.create_board('controlled')
    with closing(connect(board='controlled')) as conn:
        task_id = kb.create_task(conn, title='NO REAL PROVIDER', assignee='default',
            workspace_kind='dir', workspace_path=str(tmp_path), model_override='controlled-no-provider', provider_override='custom')
        task = kb.claim_task(conn, task_id)
    db = SessionDB(tmp_path / 'isolated-state.db')
    db.create_session('owned', 'cli')
    epoch = begin_runtime_epoch(db, instance_id='controlled-owner')
    from gateway.session_authority import SessionAuthority
    authority = SessionAuthority(SimpleNamespace(_draining=False), db=db, epoch=epoch,
        profile_id=str(tmp_path), instance_id='controlled-owner')
    actor = Principal('fixture-owner', str(tmp_path), frozenset({'session:create'}), 'transport')
    policy, _ = build_kanban_policy(SimpleNamespace(authority=authority, actor=actor, native_owner=True),
        dict(board='controlled', task_id=task.id, run_id=task.current_run_id, claim_lock=task.claim_lock),
        {'model': {'default': 'controlled-no-provider', 'provider': 'custom'}, 'platform_toolsets': {'cli': []}})
    session_id = 'owned'
    if persisted:
        from gateway.session import SessionStore
        from gateway.config import GatewayConfig
        from gateway.session_local import stage_local_session
        from hermes_state_local import commit_local_session
        authority.runner.session_store = SessionStore(tmp_path / 'routing', GatewayConfig())
        staged = stage_local_session(authority, actor, {'request_id': 'controlled-persisted-create'},
            trusted_policy=policy, trusted_secrets={})
        commit_local_session(db, epoch=epoch, receipt=staged)
        session_id = staged['session_id']
    admitted = admit_session_input(db, epoch=epoch, principal_id='fixture-owner', session_id=session_id,
        request_id='controlled-input', payload={'kanban_card_v1': {'board': 'controlled', 'task_id': task.id, 'run_id': task.current_run_id}})
    claimed = claim_session_input(db, epoch=epoch, session_id=session_id)
    return authority, json.loads(policy.kanban_json), admitted | {'generation': claimed['generation']}


@pytest.mark.platforms('posix')
def test_fresh_freeze_denies_discovery_hooks_auxiliary_and_shadowed_stdlib(tmp_path):
    root = Path(__file__).resolve().parents[2]
    blocked = ['run_agent', 'model_tools', 'agent.tool_executor', 'agent.inline_tool_executors',
        'hermes_cli.plugins', 'hermes_cli.plugins_dispatch', 'agent.auxiliary_client',
        'agent.memory_manager', 'tools.delegate_tool', 'tools.mcp_tool', 'tools.browser_tool',
        'gateway.session_worker', 'hermes_state', 'providers']
    code = f'''
import sys
sys.path.insert(0, {str(root)!r})
from hermes_protected import freeze
freeze()
import agent.protected_conversation
for name in {blocked!r}:
    try:
        __import__(name)
    except ImportError:
        pass
    else:
        raise AssertionError(name)
assert 'agent.jiter_preload' not in sys.modules
assert 'model_tools' not in sys.modules
print('FROZEN_NO_DISCOVERY')
'''
    result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code], cwd=tmp_path,
        env={'LANG': 'C', 'LC_ALL': 'C'}, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'FROZEN_NO_DISCOVERY'


def test_all_executor_bypass_entries_fail_closed_for_protected_agent():
    from agent import tool_executor as executor
    agent = SimpleNamespace(_protected_turn=object())
    entries = ['execute_tool_calls_concurrent', 'execute_tool_calls_sequential',
        '_execute_tool_calls_sequential', 'execute_tool_calls_segmented']
    for name in entries:
        with pytest.raises(RuntimeError, match='protected dispatch'):
            getattr(executor, name)(agent, None, [], '')
    with pytest.raises(RuntimeError, match='protected dispatch'):
        executor._resolve_sequential_dispatch(agent, None, [])


def test_capability_channels_reject_replay_scope_substitution_and_general_rpc():
    from gateway.session_protected_capabilities import ScopedInference
    from agent.transports.types import NormalizedResponse
    from hermes_state_runtime import RuntimeStoreError
    seen = []
    endpoint = ScopedInference(lambda m, t: seen.append(m) or NormalizedResponse('CONTROLLED', None, 'stop'), {'session_id': 'owned', 'generation': 1})
    client = endpoint.client()
    frame = dict(channel='inference', token=client._token, binding=client.binding, sequence=1,
        operation='complete', payload={'messages': [], 'tools': []})
    for delta in ({'channel': 'lifecycle'}, {'token': 'wrong'}, {'binding': {'session_id': 'foreign'}},
                  {'binding': {'session_id': 'owned', 'generation': True}},
                  {'sequence': True}, {'sequence': 2}, {'extra': 1}):
        with pytest.raises(RuntimeStoreError):
            endpoint.request(frame | delta)
    assert not seen
    assert endpoint.request(frame).content == 'CONTROLLED'
    with pytest.raises(RuntimeStoreError):
        endpoint.request(frame)
    for operation in ('worker.adopt', 'execution.finish', 'child.reserve', 'tool_call', 'provider.select'):
        with pytest.raises(RuntimeStoreError):
            client._sequence = endpoint._sequence
            client.request(operation, {})
    assert len(seen) == 1


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('invalidate', ['card', 'generation', 'epoch', 'admission', 'stop'])
def test_native_fences_reject_inflight_model_result_before_any_tool(tmp_path, monkeypatch, invalidate):
    from agent.transports.types import NormalizedResponse, ToolCall
    from gateway.session_managed_worker import _spawn_registered_worker
    from gateway.session_protected_tools import SentinelLaunch, ToolProxy
    from gateway.session_protected_capabilities import NativeLifecycle, ScopedInference
    from agent.protected_conversation import ProtectedTurn
    from hermes_state_runtime import RuntimeStoreError
    authority, context, admission = native_claim(tmp_path, monkeypatch)
    async def exercise():
        worker, process = await _spawn_registered_worker(authority, SimpleNamespace(session_id='owned'),
            {'generation': admission['generation']}, sentinel=SentinelLaunch(tmp_path.resolve()))
        proxy = ToolProxy(worker, SentinelLaunch(tmp_path.resolve()))
        owner = None
        try:
            await asyncio.to_thread(proxy.bind)
            owner = await asyncio.to_thread(NativeLifecycle.reserve, authority, admission_id=admission['admission_id'],
                principal_id='fixture-owner', process=process, context=context)
            def controlled_inference(messages, tools):
                if invalidate == 'card':
                    from hermes_cli.kanban_db_connect import connect
                    from hermes_cli import kanban_db as kb
                    with closing(connect(board='controlled')) as conn:
                        kb.block_task(conn, context['task_id'], reason='controlled cancellation', expected_run_id=context['run_id'])
                elif invalidate == 'stop':
                    worker.stop.set()
                else:
                    def revoke(conn):
                        if invalidate == 'generation':
                            conn.execute("UPDATE sessions SET runtime_generation=runtime_generation+1 WHERE id='owned'")
                        elif invalidate == 'epoch':
                            conn.execute('UPDATE worker_executions SET owner_epoch=owner_epoch+1')
                        else:
                            conn.execute("UPDATE session_admissions SET status='unknown'")
                    authority.db._execute_write(revoke)
                return NormalizedResponse(None, [ToolCall('never-execute', 'terminal', '{}')], 'tool_calls')
            turn = ProtectedTurn(ScopedInference(controlled_inference, owner.binding).client(), owner.client(), proxy)
            with pytest.raises((RuntimeStoreError, RuntimeError)):
                await asyncio.to_thread(turn.run, 'CONTROLLED_NEGATIVE')
            assert proxy.sequence == 0
            assert process.poll() is not None
            assert len(authority.db.get_messages('owned')) == 1
        finally:
            if owner is not None:
                await asyncio.to_thread(owner.close)
            worker.close()
            authority.db.close()
    asyncio.run(exercise())


@pytest.mark.platforms('posix')
def test_freeze_rejects_actual_stdlib_shadow_before_import_execution(tmp_path):
    root = Path(__file__).resolve().parents[2]
    marker = tmp_path / 'should-not-exist'
    (tmp_path / 'colorsys.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    code = f'''
import sys
sys.path.insert(0, {str(root)!r})
from hermes_protected import freeze
sys.path.insert(0, {str(tmp_path)!r})
freeze()
try:
    import colorsys
except ImportError:
    print('DENIED_SHADOW')
else:
    raise AssertionError('stdlib shadow executed')
'''
    result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code], cwd=tmp_path,
        env={'LANG': 'C', 'LC_ALL': 'C'}, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'DENIED_SHADOW' and not marker.exists()

@pytest.mark.platforms('posix')
def test_failed_native_adoption_retires_reserved_execution_without_fake_success(tmp_path, monkeypatch):
    from gateway.session_managed_worker import _spawn_registered_worker
    from gateway.session_protected_tools import SentinelLaunch, ToolProxy
    from gateway.session_protected_capabilities import NativeLifecycle
    from hermes_state_runtime import RuntimeStoreError, get_session_admission
    authority, context, admission = native_claim(tmp_path, monkeypatch)
    def refused(*args, **kwargs):
        raise RuntimeStoreError('controlled_adoption_failure')
    monkeypatch.setattr('hermes_state_runtime.adopt_worker_execution', refused)
    async def exercise():
        worker, process = await _spawn_registered_worker(authority, SimpleNamespace(session_id='owned'),
            {'generation': admission['generation']}, sentinel=SentinelLaunch(tmp_path.resolve()))
        try:
            ToolProxy(worker, SentinelLaunch(tmp_path.resolve())).bind()
            with pytest.raises(RuntimeStoreError, match='controlled_adoption_failure'):
                NativeLifecycle.reserve(authority, admission_id=admission['admission_id'],
                    principal_id='fixture-owner', process=process, context=context)
            assert get_session_admission(authority.db, admission_id=admission['admission_id'])['status'] == 'unknown'
            with authority.db._read_ctx() as conn:
                assert conn.execute('SELECT status FROM worker_executions').fetchone()[0] == 'terminal'
        finally:
            worker.close()
            authority.db.close()
    asyncio.run(exercise())
