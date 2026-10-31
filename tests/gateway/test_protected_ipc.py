"""Actual-channel negative controls, controlled peers, no real providers."""
import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

import gateway.session_protected_scope
from tests.gateway.test_protected_conversation import native_claim


@pytest.mark.platforms('posix')
def test_kernel_peer_is_not_a_claimed_pid_or_uid(tmp_path):
    from agent.protected_ipc import verify_peer
    path = str(tmp_path.parent / 'peer-control')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(path)
        listener.listen(1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(path)
            conn, _ = listener.accept()
            with conn:
                verify_peer(conn, pid=os.getpid(), uid=os.getuid())
                with pytest.raises(RuntimeError, match='peer denied'):
                    verify_peer(conn, pid=os.getppid(), uid=os.getuid())
                with pytest.raises(RuntimeError, match='peer denied'):
                    verify_peer(conn, pid=os.getpid(), uid=os.getuid() + 1)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('attack', ['replay', 'token', 'binding', 'channel', 'oversize', 'general-rpc', 'revoked', 'inference-tools'])
def test_native_owner_denies_actual_ipc_attacks(tmp_path, monkeypatch, attack):
    from gateway.session_protected_process import protected_process_scope, SelectedAsyncProvider
    from gateway.session_protected_tools import SentinelLaunch
    from agent.transports.types import NormalizedResponse
    authority, context, row = native_claim(tmp_path, monkeypatch, persisted=True)
    source = str(Path(__file__).resolve().parents[2])
    # Owner-controlled adversarial replacement for THIS test only, not a runtime
    # entry option. A real exec and kernel PID are used; all three channels open.
    driver = f'''
import sys
sys.path.insert(0, {source!r})
from hermes_protected import freeze
freeze()
import os
from agent.protected_ipc import receive, encode, SocketCapability
c = receive(sys.stdin.buffer.read)
sockets = [SocketCapability(c['path'], c['channels'][name], owner_pid=os.getppid(), owner_uid=os.getuid()) for name in ('inference','lifecycle','tools')]
lifecycle = sockets[1]
frame = dict(channel='lifecycle', token=lifecycle.client._token, binding=lifecycle.client.binding, sequence=1, operation='check', payload={{}})
attack = {attack!r}
if attack == 'replay':
    lifecycle.socket.sendall(encode(frame))
    assert receive(lifecycle.socket.recv)['ok'] is True
elif attack == 'token':
    frame['token'] = c['channels']['inference']['token']
elif attack == 'binding':
    frame['binding'] = dict(frame['binding'], epoch='foreign-epoch')
elif attack == 'channel':
    frame['channel'] = 'inference'
elif attack == 'general-rpc':
    frame['operation'] = 'worker.adopt'
elif attack == 'inference-tools':
    lifecycle = sockets[0]
    frame.update(channel='inference', token=lifecycle.client._token, operation='complete',
        payload={{'messages': [{{'role': 'user', 'content': 'CONTROLLED'}}], 'tools': []}})
if attack == 'oversize':
    import struct
    lifecycle.socket.sendall(struct.pack('!I', 65537))
else:
    lifecycle.socket.sendall(encode(frame))
reply = receive(lifecycle.socket.recv)
assert reply == {{'ok': False, 'value': None}}, reply
for channel in sockets:
    channel.close()
sys.stdout.buffer.write(encode({{'result': {{'denied': attack}}, 'imports': sorted(sys.modules)}}))
sys.stdout.buffer.flush()
'''
    actual_spawn = subprocess.Popen
    class ControlledPopen(actual_spawn):
        def __init__(self, argv, *args, **kwargs):
            if str(argv[-1]).endswith('hermes_protected_entry.py'):
                argv = argv[:-1] + ['-c', driver]
            super().__init__(argv, *args, **kwargs)
    monkeypatch.setattr('gateway.session_protected_process.subprocess.Popen', ControlledPopen)
    async def exercise():
        requests = []
        class Transport:
            def build_kwargs(self, model, messages, tools):
                return dict(messages=messages, tools=tools)
            def normalize_response(self, value):
                return value
        async def create(**kwargs):
            requests.append(kwargs)
            return NormalizedResponse('NOT_REACHED', None, 'stop')
        async def close():
            pass
        provider = SelectedAsyncProvider(model='controlled-no-provider', provider_id='custom', transport=Transport(),
            create=create, aclose=close)
        async with protected_process_scope(authority, SimpleNamespace(session_id=row['target_session_id']), row,
                SentinelLaunch(tmp_path.resolve()), principal_id='fixture-owner', context=context, provider=provider) as runner:
            if attack == 'revoked':
                runner.endpoints['lifecycle']._closing = True
            result = await runner.run_conversation('controlled attack')
            assert result == {'denied': attack}
            assert not requests
            assert runner.turn.proxy.sequence == 0
            assert authority.db.get_messages(row['target_session_id']) == []
        assert runner.process.poll() is not None
        assert runner.turn.proxy.worker.process.poll() is not None
        assert not any(t.is_alive() for t in runner.threads)
    try:
        asyncio.run(exercise())
    finally:
        authority.db.close()


@pytest.mark.platforms('posix')
def test_close_wakes_unclaimed_channel_acceptors(tmp_path, monkeypatch):
    from gateway.session_protected_process import protected_process_scope, SelectedAsyncProvider
    from gateway.session_protected_tools import SentinelLaunch
    authority, context, row = native_claim(tmp_path, monkeypatch, persisted=True)
    actual_spawn = subprocess.Popen
    class SilentPopen(actual_spawn):
        def __init__(self, argv, *args, **kwargs):
            if str(argv[-1]).endswith('hermes_protected_entry.py'):
                argv = argv[:-1] + ['-c', 'import threading; threading.Event().wait(10)']
            super().__init__(argv, *args, **kwargs)
    monkeypatch.setattr('gateway.session_protected_process.subprocess.Popen', SilentPopen)
    async def create(**kwargs):
        pytest.fail('silent child reached provider')
    async def close():
        pass
    async def exercise():
        provider = SelectedAsyncProvider(model='controlled-no-provider', provider_id='custom',
            transport=object(), create=create, aclose=close)
        async with protected_process_scope(authority, SimpleNamespace(session_id=row['target_session_id']), row,
                SentinelLaunch(tmp_path.resolve()), principal_id='fixture-owner', context=context, provider=provider) as runner:
            await runner._start('CONTROLLED_SILENT_CHILD')
            assert all(t.is_alive() for t in runner.threads)
            await asyncio.wait_for(runner.close(), 1)
            assert runner.process.poll() is not None
            assert not any(t.is_alive() for t in runner.threads)
        assert runner.turn.proxy.worker.process.poll() is not None
    try:
        asyncio.run(exercise())
    finally:
        authority.db.close()


@pytest.mark.platforms('posix')
def test_thread_start_failure_reaps_and_closes_unbootstrapped_child(tmp_path, monkeypatch):
    from gateway.session_protected_process import protected_process_scope, SelectedAsyncProvider
    from gateway.session_protected_tools import SentinelLaunch
    from agent import memory_provider
    authority, context, row = native_claim(tmp_path, monkeypatch, persisted=True)
    actual_thread = memory_provider.spawn_context_thread
    def broken_thread(*args, **kwargs):
        thread = actual_thread(*args, **kwargs)
        def fail():
            raise OSError('controlled channel thread start failure')
        thread.start = fail
        return thread
    monkeypatch.setattr(memory_provider, 'spawn_context_thread', broken_thread)
    closed = []
    async def create(**kwargs):
        pytest.fail('unbootstrapped child reached provider')
    async def close():
        closed.append(True)
    async def exercise():
        provider = SelectedAsyncProvider(model='controlled-no-provider', provider_id='custom',
            transport=object(), create=create, aclose=close)
        captured = []
        with pytest.raises(OSError, match='thread start failure'):
            async with protected_process_scope(authority, SimpleNamespace(session_id=row['target_session_id']), row,
                    SentinelLaunch(tmp_path.resolve()), principal_id='fixture-owner', context=context, provider=provider) as runner:
                captured.append(runner)
                await runner.run_conversation('CONTROLLED_START_FAILURE')
        runner = captured[0]
        assert runner.process.poll() is not None
        assert runner.process.stdin.closed and runner.process.stdout.closed
        assert runner.turn.proxy.worker.process.poll() is not None
        assert not any(t.is_alive() for t in runner.threads)
        assert closed == [True]
    try:
        asyncio.run(exercise())
    finally:
        authority.db.close()
