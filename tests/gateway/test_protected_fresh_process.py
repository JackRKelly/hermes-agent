"""Fresh frozen interpreter + real IPC, controlled async provider (NO NETWORK)."""
import asyncio
import os
from types import SimpleNamespace

import pytest

# The owner is ordinary trusted code; only its conversation child is frozen.
import gateway.session_protected_scope
from tests.gateway.test_protected_conversation import native_claim


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('mode', ['success', 'existing-transport', 'concurrent-close', 'provider-error', 'cancel', 'cancel-close-error', 'close-error', 'stop', 'retirement-error'])
def test_fresh_conversation_uses_owner_channels_and_cancels_call(tmp_path, monkeypatch, mode):
    from gateway.session_protected_process import protected_process_scope, SelectedAsyncProvider
    from gateway.session_protected_tools import SentinelLaunch
    from agent.transports.types import NormalizedResponse, ToolCall
    from hermes_state_runtime import get_session_admission
    authority, context, admission = native_claim(tmp_path, monkeypatch, persisted=True)
    session_id = admission['target_session_id']
    (tmp_path / 'allowed.txt').write_text('FRESH_NONSECRET\n')
    async def exercise():
        entered, stopped, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        class ControlledTransport:
            def build_kwargs(self, model, messages, tools):
                assert model == 'controlled-no-provider'
                return dict(messages=messages, tools=tools)
            def normalize_response(self, response):
                assert type(response) is NormalizedResponse
                return response
        def response(content, tool_calls, finish_reason):
            if mode != 'existing-transport':
                return NormalizedResponse(content, tool_calls, finish_reason)
            # Existing SDK response type + Hermes' real transport, NO SDK client
            # construction, credentials or real provider request.
            from openai.types.chat import ChatCompletion
            calls = None if tool_calls is None else [dict(id=t.id, type='function',
                function=dict(name=t.name, arguments=t.arguments)) for t in tool_calls]
            return ChatCompletion(id='controlled', created=0, object='chat.completion',
                model='controlled-no-provider', choices=[dict(index=0, finish_reason=finish_reason,
                message=dict(role='assistant', content=content, tool_calls=calls))])
        async def create(**kwargs):
            entered.set()
            calls.append(kwargs)
            if mode == 'provider-error':
                raise RuntimeError('CONTROLLED_PROVIDER_ERROR')
            if mode in ('cancel', 'cancel-close-error', 'stop'):
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            if len(calls) == 1:
                return response(None, [ToolCall('fresh-call', 'read_file', '{}')], 'tool_calls')
            assert kwargs['messages'][-1]['content'] == '1|FRESH_NONSECRET'
            return response('FRESH_DONE', None, 'stop')
        close_calls = []
        async def aclose():
            close_calls.append(True)
            closed.set()
            if mode in ('close-error', 'cancel-close-error'):
                raise OSError('controlled provider close failure')
        transport = ControlledTransport()
        if mode == 'existing-transport':
            from agent.transports.chat_completions import ChatCompletionsTransport
            transport = ChatCompletionsTransport()
        provider = SelectedAsyncProvider(model='controlled-no-provider', provider_id='custom', transport=transport,
            create=create, aclose=aclose)
        captured = []
        from gateway import session_worker_reservation as reservation
        actual_retire = reservation.lose_admission_worker
        if mode == 'retirement-error':
            def fail_retirement(*args):
                raise OSError('controlled retirement storage failure')
            monkeypatch.setattr(reservation, 'lose_admission_worker', fail_retirement)
        async def owned():
            async with protected_process_scope(authority, SimpleNamespace(session_id=session_id), admission,
                    SentinelLaunch(tmp_path.resolve()), principal_id='fixture-owner', context=context,
                    provider=provider) as runner:
                captured.append(runner)
                result = await runner.run_conversation('FRESH_CONTROLLED')
                if mode == 'concurrent-close':
                    await asyncio.gather(runner.close(), runner.close())
                return result
        async def cancelled():
            task = asyncio.create_task(owned())
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            # Repeated cancellation races eager error cleanup and scope close.
            asyncio.get_running_loop().call_later(.001, task.cancel)
            expected_error = OSError if mode == 'cancel-close-error' else asyncio.CancelledError
            with pytest.raises(expected_error):
                await task
            assert stopped.is_set()
            assert len(calls) == 1
            assert not provider._tasks

        async def stop_case():
            task = asyncio.create_task(owned())
            await asyncio.wait_for(entered.wait(), 3)
            generation, worker = authority.adopted[session_id]
            assert generation == admission['generation']
            worker.interrupt()
            with pytest.raises(RuntimeError, match='protected conversation stopped'):
                await asyncio.wait_for(task, 1)
            assert stopped.is_set() and not provider._tasks
            assert len(calls) == 1

        async def provider_failed():
            with pytest.raises(RuntimeError, match='protected conversation failed'):
                await owned()
            assert entered.is_set() and len(calls) == 1

        async def close_failed():
            with pytest.raises(OSError, match='provider close failure'):
                await owned()
            assert len(calls) == 2

        async def retirement_failed():
            with pytest.raises(OSError, match='retirement storage failure'):
                await owned()
            assert len(calls) == 2
            assert len(authority._protected_retirements) == 1
            owner = next(iter(authority._protected_retirements.values()))
            assert owner is captured[0].turn._native_owner
            assert not owner._closed and owner._closing
            monkeypatch.setattr(reservation, 'lose_admission_worker', actual_retire)
            from gateway.session_protected_scope import retry_protected_retirements
            await retry_protected_retirements(authority)
            assert not authority._protected_retirements

        async def succeeded():
            result = await owned()
            assert result['final_response'] == 'FRESH_DONE'
            assert result['api_calls'] == 2
            assert len(authority.db.get_messages(session_id)) == 4
            imports = captured[0].imports
            assert 'run_agent' not in imports and 'agent.jiter_preload' not in imports
            assert not any(name.startswith(('model_tools', 'providers', 'tools', 'plugins')) for name in imports)
        scenarios = {'cancel': cancelled, 'cancel-close-error': cancelled, 'stop': stop_case,
            'provider-error': provider_failed, 'close-error': close_failed, 'retirement-error': retirement_failed,
            'success': succeeded, 'existing-transport': succeeded, 'concurrent-close': succeeded}
        await scenarios[mode]()
        runner = captured[0]
        assert runner.process.pid != os.getpid()
        assert runner.process.poll() is not None
        assert runner.turn.proxy.worker.process.poll() is not None
        assert not any(thread.is_alive() for thread in runner.threads)
        assert closed.is_set() and close_calls == [True]
        assert authority._managed_workers == {}
        assert get_session_admission(authority.db, admission_id=admission['admission_id'])['status'] == 'unknown'
    try:
        asyncio.run(exercise())
    finally:
        authority.db.close()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('invalid', ['missing-policy', 'context', 'workspace', 'principal', 'generation', 'stop'])
def test_owner_rejects_policy_or_prespawn_stop_before_any_child(tmp_path, monkeypatch, invalid):
    from gateway.session_protected_process import protected_process_scope, SelectedAsyncProvider
    from gateway.session_protected_tools import SentinelLaunch
    from hermes_state_runtime import RuntimeStoreError
    authority, context, admission = native_claim(tmp_path, monkeypatch, persisted=invalid != 'missing-policy')
    sid = admission['target_session_id']
    principal = 'fixture-owner'
    workspace = tmp_path.resolve()
    if invalid == 'context':
        context = dict(context, claim_lock='wrong')
    if invalid == 'workspace':
        workspace = tmp_path / 'other'
        workspace.mkdir()
    if invalid == 'principal':
        principal = 'foreign-owner'
    if invalid == 'generation':
        admission = dict(admission, generation=admission['generation'] + 1)
    if invalid == 'stop':
        authority.pending_stops[sid] = admission['generation']
    async def create(**kwargs):
        pytest.fail('provider reached before owner binding')
    closed = []
    async def close():
        closed.append(True)
    def refuse_spawn(*args, **kwargs):
        pytest.fail('child spawned before owner binding/Stop check')
    monkeypatch.setattr('gateway.session_managed_worker.subprocess.Popen', refuse_spawn)
    async def exercise():
        provider = SelectedAsyncProvider(model='controlled-no-provider', provider_id='custom',
            transport=object(), create=create, aclose=close)
        with pytest.raises(RuntimeStoreError):
            async with protected_process_scope(authority, SimpleNamespace(session_id=sid), admission,
                    SentinelLaunch(workspace), principal_id=principal, context=context, provider=provider):
                pytest.fail('invalid owner scope yielded')
        assert not getattr(authority, '_managed_workers', {})
        assert closed == [True]
    try:
        asyncio.run(exercise())
    finally:
        authority.db.close()
