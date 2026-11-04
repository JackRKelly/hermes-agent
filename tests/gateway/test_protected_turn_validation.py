"""Pure restricted-route validation with explicitly controlled in-memory doubles."""
from types import SimpleNamespace
import threading

import pytest

from agent.transports.types import NormalizedResponse, ToolCall
from agent.protected_conversation import ProtectedTurn


def controlled_turn(response):
    rows, calls, closes = [], [], []
    binding = {'session_id': 'unit-only', 'generation': 1}
    def lifecycle(operation, payload):
        if operation == 'append':
            rows.extend(payload['messages'])
    proxy = SimpleNamespace(worker=SimpleNamespace(stop=threading.Event(), close=lambda: closes.append(True)),
        invoke=lambda name, args: calls.append(name) or 'NONSECRET_UNIT_RESULT')
    inference = SimpleNamespace(channel='inference', binding=binding, request=lambda *a: response)
    owner = SimpleNamespace(channel='lifecycle', binding=binding, request=lifecycle)
    return ProtectedTurn(inference, owner, proxy), rows, calls, closes


@pytest.mark.parametrize('response', [
    {}, NormalizedResponse('x', None, 'length'), NormalizedResponse('x', None, 'stop', reasoning='x'),
    NormalizedResponse('x', None, 'stop', provider_data={'route': 'forbidden'}),
    NormalizedResponse('x' * 4097, None, 'stop'), NormalizedResponse(None, None, 'stop'),
    NormalizedResponse('x', None, 'tool_calls'),
    NormalizedResponse(None, [ToolCall('a', 'read_file', '{}')], 'stop'),
])
def test_rejects_unsupported_provider_responses_without_tool_fallback(response):
    turn, rows, calls, closes = controlled_turn(response)
    with pytest.raises(ValueError):
        turn.run('UNIT_ONLY')
    assert not calls and len(rows) == 1 and closes


@pytest.mark.parametrize('call', [
    ToolCall(None, 'read_file', '{}'), ToolCall(True, 'read_file', '{}'), ToolCall('', 'read_file', '{}'),
    ToolCall('x', 'write_file', '{}'), ToolCall('x', 'terminal', '{"command":"forbidden"}'),
    ToolCall('x', 'read_file', '{}', {'signature': 'unsupported'}),
    SimpleNamespace(id='x', name='read_file', arguments='{}', provider_data=None),
])
def test_tool_schema_rejections_are_batch_atomic(call):
    response = NormalizedResponse(None, [ToolCall('good', 'read_file', '{}'), call], 'tool_calls')
    turn, rows, calls, _ = controlled_turn(response)
    with pytest.raises(ValueError):
        turn.run('UNIT_ONLY')
    assert not calls and len(rows) == 1


def test_binding_types_are_exact_not_python_numeric_equality():
    turn, _, _, _ = controlled_turn(NormalizedResponse('ok', None, 'stop'))
    turn.inference.binding = {'session_id': 'unit-only', 'generation': True}
    with pytest.raises(ValueError):
        ProtectedTurn(turn.inference, turn.lifecycle, turn.proxy)


def test_single_use_and_bad_prompts_fail_closed():
    for prompt in ('', 1, 'x' * 4097):
        turn, rows, calls, closes = controlled_turn(NormalizedResponse('ok', None, 'stop'))
        with pytest.raises(ValueError):
            turn.run(prompt)
        assert not rows and not calls and closes
    turn, _, _, _ = controlled_turn(NormalizedResponse('ok', None, 'stop'))
    assert turn.run('UNIT_ONLY')['completed']
    with pytest.raises(RuntimeError, match='single use'):
        turn.run('UNIT_ONLY')


def test_expired_turn_never_calls_inference_or_tools():
    turn, rows, calls, closes = controlled_turn(NormalizedResponse('ok', None, 'stop'))
    turn.deadline = 0
    with pytest.raises(RuntimeError, match='expired'):
        turn.run('UNIT_ONLY')
    assert not rows and not calls and closes
