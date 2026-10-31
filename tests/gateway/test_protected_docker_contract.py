"""Hermetic Docker authority/argument contracts; no daemon or provider needed."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import gateway.session_protected_scope

from agent.protected_conversation import ProtectedTurn, tool_arguments
from agent.transports.types import NormalizedResponse, ToolCall
from gateway.session_protected_docker import DockerContainer, DockerLaunch, DockerRuntime
from gateway.session_protected_observations import OwnerObservations
from hermes_state_runtime import RuntimeStoreError


@pytest.mark.parametrize('raw', ['{"command":"a","command":"b"}', '{"command":"a","extra":1}',
    json.dumps({'command': 'x' * 2049}), json.dumps({'command': '\x00'}), '[]', 'true'])
def test_closed_argument_validation(raw):
    with pytest.raises(ValueError):
        tool_arguments('terminal', raw, True)


def test_argument_substitution_and_legacy_nonempty_refuse_before_tools():
    from gateway.session_protected_tools import ToolProxy
    from gateway.session_protected_process import _Tools
    from tests.gateway.test_protected_observations import append
    tools = [{'function': {'description': 'Credentialless Docker-only native tool'}}]
    value = OwnerObservations(tools)
    value.start('trusted prompt')
    append(value, 1, {'role': 'user', 'content': 'trusted prompt'})
    value.before_inference(value.messages, tools)
    value.observe_response(NormalizedResponse(None, [ToolCall('chosen', 'terminal', '{"command":"printf GOOD"}')], 'tool_calls'))
    append(value, 2, {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'chosen', 'type': 'function', 'function': {'name': 'terminal', 'arguments': '{"command":"printf GOOD"}'}}]})
    for args in ({'command': 'printf BAD'}, {'path': '/owner'}, {}, {'command': 'printf GOOD', 'extra': 1}):
        with pytest.raises(RuntimeStoreError):
            value.before_tool('terminal', args)
    assert value.before_tool('terminal', {'command': 'printf GOOD'}) == 'chosen'
    for name in ('terminal', 'read_file'):
        assert tool_arguments(name, '{}') == {}
        with pytest.raises(ValueError):
            tool_arguments(name, json.dumps({'command' if name == 'terminal' else 'path': 'BAD'}))
    # Actual proxy's non-Docker gate, without launching a cooperative child.
    worker = SimpleNamespace(stop=SimpleNamespace(is_set=lambda: False), container=None)
    proxy = ToolProxy(worker, None)
    proxy.bound = True
    with pytest.raises(ValueError):
        proxy.invoke('terminal', {'command': 'printf BAD'})
    # Owner ledger used by the real _Tools endpoint refuses unobserved args.
    plain = OwnerObservations([])
    plain.start('legacy')
    append(plain, 1, {'role': 'user', 'content': 'legacy'})
    plain.before_inference(plain.messages, [])
    plain.observe_response(NormalizedResponse(None, [ToolCall('legacy', 'terminal', '{}')], 'tool_calls'))
    append(plain, 2, {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'legacy', 'type': 'function', 'function': {'name': 'terminal', 'arguments': '{}'}}]})
    turn = SimpleNamespace(proxy=SimpleNamespace(worker=worker), _native_owner=SimpleNamespace(_observations=plain, _check=lambda: None))
    endpoint = _Tools.__new__(_Tools)
    endpoint.turn = turn
    with pytest.raises(RuntimeStoreError):
        endpoint._invoke('call', {'name': 'terminal', 'args': {'command': 'printf BAD'}})
    # Frozen conversation rejects nonempty args before appending or invoking.
    noop = SimpleNamespace(binding={}, channel='inference')
    turn = ProtectedTurn(noop, SimpleNamespace(binding={}, channel='lifecycle'), proxy)
    with pytest.raises(ValueError):
        turn.dispatch(NormalizedResponse(None, [ToolCall('bad', 'terminal', '{"command":"BAD"}')], 'tool_calls'), [])


def test_docker_label_generation_must_equal_native_reserved_generation():
    from gateway.session_protected_capabilities import NativeLifecycle
    owner = NativeLifecycle.__new__(NativeLifecycle)
    owner._scope = {'generation': 7}
    owner._container_binding = {'generation': '8'}
    owner._process = SimpleNamespace(_protected_container=SimpleNamespace(attest=lambda: {'generation': '8'}))
    with pytest.raises(RuntimeStoreError, match='protected_container_binding_mismatch'):
        owner._check()


def runtime_stub():
    runtime = DockerRuntime.__new__(DockerRuntime)
    runtime.daemon, runtime.endpoint = 'test-daemon', 'unix:///test.sock'
    runtime.image, runtime.source = 'sha256:' + 'a' * 64, 'b' * 64
    runtime.pending, runtime.evidence = {}, None
    runtime.json = lambda args: runtime.daemon
    runtime.run = lambda args, **kwargs: (1, b'error: no such object')
    return runtime


def lease(tmp_path, runtime, suffix, state='PENDING_CREATE', image=None):
    nonce = suffix * 32
    name = 'hermes-tool-' + nonce + '-1'
    value = dict(state=state, name=name, cid=None, image=image or runtime.image,
        source=runtime.source, endpoint=runtime.endpoint, daemon=runtime.daemon)
    value['labels'] = {'hermes.tool.lease': nonce, 'hermes.tool.generation': '1',
        'hermes.tool.image': value['image'], 'hermes.tool.source': runtime.source, 'hermes.tool.role': 'builder'}
    (tmp_path / (name + '.json')).write_text(json.dumps(value), encoding='utf-8')
    return name


def test_reconcile_retired_foreign_and_never_created_leases(tmp_path):
    runtime = runtime_stub()
    lease(tmp_path, runtime, '1', 'RETIRED', 'sha256:' + 'f' * 64)
    pending = lease(tmp_path, runtime, '2')
    runtime.reconcile(tmp_path)
    assert json.loads((tmp_path / (pending + '.json')).read_text(encoding='utf-8'))['state'] == 'RETIRED'


def test_reconcile_collects_errors_and_attempts_every_lease(tmp_path, monkeypatch):
    runtime = runtime_stub()
    first = lease(tmp_path, runtime, '1')
    second = lease(tmp_path, runtime, '2')
    attempted = []
    def close(self):
        attempted.append(self.name)
        if self.name == first:
            raise OSError('controlled first lease fault')
    monkeypatch.setattr(DockerContainer, 'close', close)
    with pytest.raises(RuntimeError, match='reconciliation'):
        runtime.reconcile(tmp_path)
    assert set(attempted) == {first, second}


def test_refused_input_cannot_leave_a_lease(tmp_path):
    import asyncio
    runtime = runtime_stub()
    (tmp_path / 'allowed.txt').symlink_to('/not-approved')
    directory = tmp_path / 'leases'
    launch = DockerLaunch(tmp_path.resolve(), runtime, directory, ('allowed.txt',), 'builder')
    with pytest.raises(ValueError):
        asyncio.run(launch.acquire(1))
    assert not directory.exists() or not list(directory.glob('*.json'))


@pytest.mark.platforms('posix')
def test_container_close_fault_still_reaps_bridge_and_retry_keeps_exact_worker():
    import subprocess
    import sys
    from gateway.session_protected_tools import SentinelWorker
    process = subprocess.Popen([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(60)'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    authority = SimpleNamespace(_managed_workers={})
    worker = SentinelWorker(process, authority, 'private')
    authority._managed_workers['private'] = worker
    def fail():
        raise OSError('controlled Docker close failure')
    worker.container = SimpleNamespace(close=fail)
    try:
        with pytest.raises(OSError):
            worker.close()
        assert process.poll() is not None
        assert authority._managed_workers['private'] is worker
        worker.container.close = lambda: None
        worker.close()
        assert not authority._managed_workers
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
