"""Policy-bound pilot timing; controlled peers only, never live providers."""
from dataclasses import asdict

import pytest

# Owner bootstrap before per-test home guards, never inside the guest.
import gateway.session_protected_scope


def test_pilot_timing_is_closed_typed_policy_not_a_worker_flag():
    import gateway.session_protected_native_sdk as native
    assert 'pilot_timing' in native.NativeSDKPolicy.__dataclass_fields__, 'missing signed timing policy'
    from gateway.session_protected_timing import PilotTiming
    timing = PilotTiming()
    timing.validate()
    assert asdict(timing) == dict(request_seconds=10.0, turn_seconds=25.0, tool_seconds=5.0,
        shell_seconds=2.0, namespace_seconds=30.0, close_seconds=1.0)
    policy = native.NativeSDKPolicy(provider='anthropic', model='controlled-model', session_id='controlled',
        base_url='https://api.anthropic.com', accounting='subscription_request_bound',
        request_seconds=10, pilot_timing=timing)
    policy.validate()
    assert asdict(policy)['pilot_timing'] == asdict(timing)
    assert native.NativeSDKPolicy.__dataclass_fields__['request_seconds'].default == 5
    from dataclasses import replace
    for bad in (dict(turn_seconds=31), dict(request_seconds=11), dict(tool_seconds=26),
                dict(namespace_seconds=60), dict(close_seconds=2), dict(shell_seconds=3), dict(turn_seconds=float('nan'))):
        with pytest.raises((ValueError, RuntimeError)):
            replace(timing, **bad).validate()
    for bad in (dict(pilot_timing=asdict(timing)), dict(pilot_timing=None), dict(accounting='strict')):
        with pytest.raises(RuntimeError):
            replace(policy, **bad).validate()


def test_pilot_deadline_cannot_reset_controller_or_outlive_it(monkeypatch):
    import gateway.session_protected_timing as timing
    clock = [100.0]
    monkeypatch.setattr(timing.time, 'monotonic', lambda: clock[0])
    deadline = timing.TurnDeadline(timing.PilotTiming(), 107.0)
    assert deadline.deadline == 107.0 and deadline.remaining() == 7
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        deadline.deadline = 200
    clock[0] = 106
    assert deadline.remaining() == 1
    assert deadline.binding()['deadline'] == 107
    assert hasattr(deadline, 'namespace_expires_at'), 'guest must be clamped to original controller expiry'
    clock[0] = 107
    with pytest.raises(RuntimeError, match='expired'):
        deadline.remaining()
    with pytest.raises(RuntimeError, match='expired'):
        timing.TurnDeadline(timing.PilotTiming(), 107)


def test_pilot_tool_five_seconds_includes_daemon_attestation(monkeypatch):
    import gateway.session_protected_tools as tools
    from gateway.session_protected_timing import PilotTiming, TurnDeadline
    from types import SimpleNamespace
    import threading
    clock, sent, closed = [100.0], [], []
    monkeypatch.setattr(tools.time, 'monotonic', lambda: clock[0])
    def attest(**kwargs):
        assert kwargs['seconds'] == 5
        clock[0] += 6
        return {'cid': 'controlled'}
    worker = SimpleNamespace(stop=threading.Event(), close=lambda: closed.append(True),
        container=SimpleNamespace(attest=attest))
    proxy = tools.ToolProxy(worker, None, deadline=TurnDeadline(PilotTiming(), 700))
    proxy.bound, proxy.attestation = True, {'cid': 'controlled'}
    proxy._send = sent.append
    proxy._receive = lambda: dict(op='result', sequence=1, result='LATE', imports=list(tools.SOURCE_IMPORTS))
    with pytest.raises(TimeoutError, match='tool deadline'):
        proxy.invoke('read_file', {})
    assert not sent and closed


def test_recovered_docker_lease_has_no_renewed_pilot_deadline(tmp_path, monkeypatch):
    import json
    from gateway.session_protected_docker import DockerContainer
    from tests.gateway.test_protected_docker_contract import runtime_stub, lease
    runtime = runtime_stub()
    name = lease(tmp_path, runtime, '1', state='CREATING')
    seen = []
    def close(container):
        assert container.deadline is None
        assert container.deadline_args() == ['deadline', '30']
        seen.append(container.name)
    monkeypatch.setattr(DockerContainer, 'close', close)
    runtime._reconcile_lease(tmp_path / (name + '.json'), json.loads((tmp_path / (name + '.json')).read_text()))
    assert seen == [name]
