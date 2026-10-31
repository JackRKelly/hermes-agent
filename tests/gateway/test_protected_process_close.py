"""B5/B6: actual native owner + processes, NO-REAL-PROVIDER close faults."""
import asyncio
from contextlib import suppress
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest

import gateway.session_protected_scope
from tests.gateway.test_protected_conversation import native_claim


class Transport:
    def build_kwargs(self, model, messages, tools):
        return dict(messages=messages, tools=tools)
    def normalize_response(self, value):
        return value


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('cancel_run', [False, True])
def test_close_during_actual_acquisition_drains_late_child(tmp_path, monkeypatch, cancel_run):
    from gateway.session_protected_process import protected_process_scope, SelectedAsyncProvider
    from gateway.session_protected_tools import SentinelLaunch
    from agent.transports.types import NormalizedResponse
    authority, context, row = native_claim(tmp_path, monkeypatch, persisted=True)
    entered, release = threading.Event(), threading.Event()
    actual = subprocess.Popen
    spawned = []
    class HeldPopen(actual):
        def __init__(self, argv, *args, **kwargs):
            if str(argv[-1]).endswith('hermes_protected_entry.py'):
                entered.set()
                assert release.wait(5)
                super().__init__(argv, *args, **kwargs)
                spawned.append(self)
            else:
                super().__init__(argv, *args, **kwargs)
    monkeypatch.setattr('gateway.session_protected_process.subprocess.Popen', HeldPopen)
    async def create(**kwargs):
        return NormalizedResponse('NOT_REACHED', None, 'stop')
    async def close():
        pass
    async def exercise():
        provider = SelectedAsyncProvider(model='controlled-no-provider', provider_id='custom',
            transport=Transport(), create=create, aclose=close)
        async with protected_process_scope(authority, SimpleNamespace(session_id=row['target_session_id']), row,
                SentinelLaunch(tmp_path.resolve()), principal_id='fixture-owner', context=context, provider=provider) as runner:
            running = asyncio.create_task(runner.run_conversation('held spawn'))
            closing = None
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                directory = runner._directory.name
                closing = asyncio.create_task(runner.close())
                assert await asyncio.to_thread(runner._closed.wait, 2)
                assert not closing.done(), 'close returned before its acquisition resolved'
                if cancel_run:
                    running.cancel()
                release.set()
                await asyncio.wait_for(closing, 3)
                with pytest.raises(asyncio.CancelledError if cancel_run else RuntimeError):
                    await running
                assert len(spawned) == 1
                assert spawned[0].poll() is not None
                assert spawned[0].stdin.closed and spawned[0].stdout.closed
                assert not Path(directory).exists()
                assert not any(t.is_alive() for t in runner.threads)
            finally:
                release.set()
                with suppress(BaseException):
                    await asyncio.wait_for(asyncio.shield(running), 6)
                if closing is not None:
                    with suppress(BaseException):
                        await closing
                # RED controls must not themselves leave an orphan behind.
                for process in spawned:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=2)
                    process.stdin.close()
                    process.stdout.close()
    try:
        asyncio.run(exercise())
    finally:
        release.set()
        authority.db.close()


@pytest.mark.platforms('posix')
def test_idle_frame_receive_observes_revocation_without_kernel_shutdown():
    import socket
    from gateway.session_protected_process import ProtectedProcess
    # Real sockets; no close/shutdown wakeup is supplied to the receiving thread.
    # This isolates the BSD timed-recv race observed in the unchanged Stop test.
    runner = object.__new__(ProtectedProcess)
    runner._closed = threading.Event()
    errors = []
    reader, writer = socket.socketpair()
    entered = threading.Event()
    def receive():
        entered.set()
        try:
            runner._receive(reader)
        except EOFError:
            errors.append('revoked')
    thread = threading.Thread(target=receive)
    try:
        thread.start()
        assert entered.wait(1)
        runner._closed.set()
        thread.join(timeout=.5)
        assert not thread.is_alive()
        assert errors == ['revoked']
    finally:
        writer.close()
        thread.join(timeout=6)
        reader.close()


@pytest.mark.platforms('posix')
def test_cancelled_sdk_close_is_retained_and_explicitly_retryable():
    from gateway.session_protected_process import SelectedAsyncProvider
    async def exercise():
        calls = []
        async def create(**kwargs):
            pytest.fail('close fixture must never invoke a provider')
        async def close():
            calls.append(True)
            if len(calls) == 1:
                raise asyncio.CancelledError
        provider = SelectedAsyncProvider(model='controlled-no-provider', provider_id='custom',
            transport=Transport(), create=create, aclose=close)
        with pytest.raises(asyncio.CancelledError):
            await provider.close()
        await provider.close(retry=True)
        await provider.close()
        assert calls == [True, True]
    asyncio.run(exercise())


@pytest.mark.platforms('posix')
def test_run_after_close_cannot_spawn(tmp_path, monkeypatch):
    from gateway.session_protected_process import protected_process_scope, SelectedAsyncProvider
    from gateway.session_protected_tools import SentinelLaunch
    from agent.transports.types import NormalizedResponse
    authority, context, row = native_claim(tmp_path, monkeypatch, persisted=True)
    async def create(**kwargs):
        return NormalizedResponse('NOT_REACHED', None, 'stop')
    async def close():
        pass
    async def exercise():
        provider = SelectedAsyncProvider(model='controlled-no-provider', provider_id='custom',
            transport=Transport(), create=create, aclose=close)
        async with protected_process_scope(authority, SimpleNamespace(session_id=row['target_session_id']), row,
                SentinelLaunch(tmp_path.resolve()), principal_id='fixture-owner', context=context, provider=provider) as runner:
            await runner.close()
            def forbidden(*args, **kwargs):
                pytest.fail('closed process facade attempted a spawn')
            monkeypatch.setattr('gateway.session_protected_process.subprocess.Popen', forbidden)
            with pytest.raises(RuntimeError, match='closed'):
                await runner.run_conversation('after close')
    try:
        asyncio.run(exercise())
    finally:
        authority.db.close()


@pytest.mark.platforms('posix')
def test_reap_fault_closes_other_resources_and_is_retryable(tmp_path, monkeypatch):
    from gateway.session_protected_process import protected_process_scope, SelectedAsyncProvider
    from gateway.session_protected_tools import SentinelLaunch
    from agent.transports.types import NormalizedResponse
    authority, context, row = native_claim(tmp_path, monkeypatch, persisted=True)
    async def create(**kwargs):
        return NormalizedResponse('CONTROLLED_DONE', None, 'stop')
    async def close():
        pass
    async def exercise():
        provider = SelectedAsyncProvider(model='controlled-no-provider', provider_id='custom',
            transport=Transport(), create=create, aclose=close)
        async with protected_process_scope(authority, SimpleNamespace(session_id=row['target_session_id']), row,
                SentinelLaunch(tmp_path.resolve()), principal_id='fixture-owner', context=context, provider=provider) as runner:
            assert (await runner.run_conversation('close fault'))['completed']
            directory = runner._directory.name
            thread = runner.threads[0]
            actual_join = thread.join
            attempts, reaps = [], []
            actual_reap = runner._reap_process
            def reap_once():
                reaps.append(True)
                actual_reap()
            monkeypatch.setattr(runner, '_reap_process', reap_once)
            def fail_once(*args, **kwargs):
                actual_join(*args, **kwargs)
                attempts.append(True)
                if len(attempts) == 1:
                    raise RuntimeError('controlled join failure')
            monkeypatch.setattr(thread, 'join', fail_once)
            with pytest.raises(RuntimeError, match='controlled join failure'):
                await runner.close()
            assert runner.process.poll() is not None
            assert runner.process.stdin.closed and runner.process.stdout.closed
            assert not Path(directory).exists()
            assert runner in authority._protected_process_closes.values()
            await runner.close()
            assert runner not in authority._protected_process_closes.values()
            assert len(attempts) == 2
            assert len(reaps) == 1  # retry failed join, not the already-reaped PID/group
    try:
        asyncio.run(exercise())
    finally:
        authority.db.close()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('stalled', ['create', 'aclose'])
def test_uncooperative_provider_cannot_hold_native_teardown(tmp_path, monkeypatch, stalled):
    from gateway.session_protected_process import protected_process_scope, SelectedAsyncProvider
    from gateway.session_protected_tools import SentinelLaunch
    from agent.transports.types import NormalizedResponse, ToolCall
    from hermes_state_runtime import get_session_admission
    authority, context, row = native_claim(tmp_path, monkeypatch, persisted=True)
    (tmp_path / 'allowed.txt').write_text('CONTROLLED\n')
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def ignore_cancel():
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
        async def create(**kwargs):
            if stalled == 'create':
                entered.set()
                await ignore_cancel()
                return NormalizedResponse(None, [ToolCall('late', 'read_file', '{}')], 'tool_calls')
            return NormalizedResponse('CONTROLLED_DONE', None, 'stop')
        async def aclose():
            if stalled == 'aclose':
                entered.set()
                await ignore_cancel()
        provider = SelectedAsyncProvider(model='controlled-no-provider', provider_id='custom',
            transport=Transport(), create=create, aclose=aclose)
        captured = []
        async def owned():
            async with protected_process_scope(authority, SimpleNamespace(session_id=row['target_session_id']), row,
                    SentinelLaunch(tmp_path.resolve()), principal_id='fixture-owner', context=context, provider=provider) as runner:
                captured.append(runner)
                return await runner.run_conversation('uncooperative provider')
        task = asyncio.create_task(owned())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            if stalled == 'create':
                task.cancel()
            with pytest.raises(RuntimeError, match='provider_close_timeout'):
                await asyncio.wait_for(asyncio.shield(task), 8)
            runner = captured[0]
            assert runner.process.poll() is not None
            assert runner.turn.proxy.worker.process.poll() is not None
            assert not any(t.is_alive() for t in runner.threads)
            assert get_session_admission(authority.db, admission_id=row['admission_id'])['status'] == 'unknown'
            assert authority.db._read_one('SELECT status FROM worker_executions')[0] == 'terminal'
            assert runner.turn.proxy.sequence == 0
            assert runner in authority._protected_process_closes.values()
        finally:
            release.set()
            with suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(task), 8)
            sdk_close = getattr(provider, '_sdk_close_task', None)
            if sdk_close is not None:
                with suppress(BaseException):
                    await sdk_close
            for pending in tuple(provider._tasks):
                with suppress(BaseException):
                    await pending
        from gateway.session_protected_process import retry_protected_process_closes
        await retry_protected_process_closes(authority)
        assert not authority._protected_process_closes
        assert captured[0].turn.proxy.sequence == 0
    try:
        asyncio.run(exercise())
    finally:
        authority.db.close()
