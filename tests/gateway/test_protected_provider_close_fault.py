"""B7: SDK self-cancellation is a teardown fault, not owner cancellation.

Real native claims, real children and controlled NO-REAL-PROVIDER closures only.
"""
import asyncio
from types import SimpleNamespace

import pytest
import gateway.session_protected_scope
from tests.gateway.test_protected_conversation import native_claim
from tests.gateway.test_protected_process_close import Transport


class SDKCloseError(Exception):
    pass


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('join_fault', [False, True])
@pytest.mark.parametrize('error_type', [asyncio.CancelledError, SDKCloseError])
def test_sdk_cancelled_close_is_fault_after_owned_children_reaped(tmp_path, monkeypatch, join_fault, error_type):
    from agent.transports.types import NormalizedResponse
    from gateway.session_protected_process import SelectedAsyncProvider, protected_process_scope, retry_protected_process_closes
    from gateway.session_protected_tools import SentinelLaunch
    from hermes_state_runtime import get_session_admission
    authority, context, row = native_claim(tmp_path, monkeypatch, persisted=True)
    async def exercise():
        calls, captured = [], []
        async def create(**kwargs):
            return NormalizedResponse('CONTROLLED_FINAL', None, 'stop')
        async def close():
            calls.append(True)
            if len(calls) == 1:
                raise error_type
        provider = SelectedAsyncProvider(model='controlled-no-provider', provider_id='custom',
            transport=Transport(), create=create, aclose=close)
        async def owned():
            async with protected_process_scope(authority, SimpleNamespace(session_id=row['target_session_id']), row,
                    SentinelLaunch(tmp_path.resolve()), principal_id='fixture-owner', context=context, provider=provider) as runner:
                captured.append(runner)
                await runner.run_conversation('controlled cancelled close')
                if join_fault:
                    thread = runner.threads[0]
                    actual_join, attempts = thread.join, []
                    def fail_once(*args, actual_join=actual_join, attempts=attempts, **kwargs):
                        actual_join(*args, **kwargs)
                        attempts.append(True)
                        if len(attempts) == 1:
                            raise RuntimeError('controlled join fault')
                    monkeypatch.setattr(thread, 'join', fail_once)
        task = asyncio.create_task(owned())
        with pytest.raises(RuntimeError, match='provider_close_cancelled|provider_close_failed|controlled join fault'):
            await task
        assert not task.cancelled()
        runner = captured[0]
        assert runner.process.poll() is not None
        assert runner.turn.proxy.worker.process.poll() is not None
        assert get_session_admission(authority.db, admission_id=row['admission_id'])['status'] == 'unknown'
        assert runner in authority._protected_process_closes.values()
        await retry_protected_process_closes(authority)
        assert not authority._protected_process_closes
        assert calls == [True, True]
    try:
        asyncio.run(exercise())
    finally:
        authority.db.close()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('error_type', [asyncio.CancelledError, SDKCloseError])
def test_cancelled_sdk_close_does_not_abort_other_retained_resource_retries(tmp_path, monkeypatch, error_type):
    from agent.transports.types import NormalizedResponse
    from gateway.session_protected_process import SelectedAsyncProvider, protected_process_scope, retry_protected_process_closes
    from gateway.session_protected_tools import SentinelLaunch
    # Two real native scopes, sharing only the owner-private cleanup registry.
    # Canonical stores/claims remain independent; no native rows are fabricated.
    claims = []
    for name in ('first', 'second'):
        directory = tmp_path / name
        directory.mkdir()
        claims.append((directory, *native_claim(directory, monkeypatch, persisted=True)))
    registry = {}
    for _, authority, _, _ in claims:
        authority._protected_process_closes = registry
    async def exercise():
        captured, joins = [], [[], []]
        sdk_calls, sdk_ready = [], [False]
        async def create(**kwargs):
            return NormalizedResponse('CONTROLLED_FINAL', None, 'stop')
        for index, (directory, authority, context, row) in enumerate(claims):
            async def close(index=index):
                if index == 0:
                    sdk_calls.append(True)
                    if not sdk_ready[0]:
                        raise error_type
            provider = SelectedAsyncProvider(model='controlled-no-provider', provider_id='custom',
                transport=Transport(), create=create, aclose=close)
            async def owned():
                async with protected_process_scope(authority, SimpleNamespace(session_id=row['target_session_id']), row,
                        SentinelLaunch(directory.resolve()), principal_id='fixture-owner', context=context, provider=provider) as runner:
                    captured.append(runner)
                    await runner.run_conversation('controlled retry sweep')
                    thread, attempts = runner.threads[0], joins[index]
                    actual_join = thread.join
                    def fail_once(*args, actual_join=actual_join, attempts=attempts, **kwargs):
                        actual_join(*args, **kwargs)
                        attempts.append(True)
                        if len(attempts) == 1:
                            raise RuntimeError('controlled join fault')
                    monkeypatch.setattr(thread, 'join', fail_once)
            task = asyncio.create_task(owned())
            with pytest.raises(RuntimeError, match='provider_close_cancelled|provider_close_failed|controlled join fault'):
                await task
            assert not task.cancelled()
        assert len(registry) == 2
        with pytest.raises(RuntimeError, match='provider_close_cancelled|provider_close_failed'):
            await retry_protected_process_closes(claims[0][1])
        # First retry still faults; the SECOND real runner was nonetheless retried.
        assert joins[1] == [True, True]
        assert captured[1] not in registry.values()
        assert captured[0] in registry.values()
        sdk_ready[0] = True
        await retry_protected_process_closes(claims[0][1])
        assert not registry
        assert sdk_calls == [True, True, True]
        assert all(r.process.poll() is not None and r.turn.proxy.worker.process.poll() is not None for r in captured)
    try:
        asyncio.run(exercise())
    finally:
        for _, authority, _, _ in claims:
            authority.db.close()
