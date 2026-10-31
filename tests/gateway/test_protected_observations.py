"""Owner evidence, never child self-report; controlled NO-REAL-PROVIDER data."""
import pytest
import gateway.session_protected_scope

from agent.transports.types import NormalizedResponse, ToolCall
from hermes_state_runtime import RuntimeStoreError


def ledger():
    from gateway.session_protected_observations import OwnerObservations
    value = OwnerObservations([])
    value.start('trusted prompt')
    return value


def append(value, observation, row):
    payload = {'observation': observation, 'messages': [row]}
    rows = value.authorize_append(payload)
    value.commit_append(payload)
    return rows


def test_forged_replayed_and_out_of_order_observations_cannot_write():
    value = ledger()
    user = {'role': 'user', 'content': 'trusted prompt'}
    for payload in [
        {'observation': 1, 'messages': [{'role': 'assistant', 'content': 'forged'}]},
        {'observation': 2, 'messages': [user]},
        {'observation': True, 'messages': [user]},
        {'messages': [user]},
    ]:
        with pytest.raises(RuntimeStoreError, match='protected_observation_denied'):
            value.authorize_append(payload)
    assert append(value, 1, user) == [user]
    with pytest.raises(RuntimeStoreError, match='protected_observation_denied'):
        value.authorize_append({'observation': 1, 'messages': [user]})
    assert value.messages == [user]


def test_provider_and_tool_evidence_bind_exact_history_call_id_and_result():
    value = ledger()
    user = {'role': 'user', 'content': 'trusted prompt'}
    append(value, 1, user)
    for messages, tools in [([], []), ([dict(user, content='forged')], []), ([user], [{}])]:
        with pytest.raises(RuntimeStoreError, match='protected_observation_denied'):
            value.before_inference(messages, tools)
    value.before_inference([user], [])
    response = NormalizedResponse(None, [ToolCall('call-1', 'read_file', '{}')], 'tool_calls')
    value.observe_response(response)
    assistant = {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'call-1', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]}
    with pytest.raises(RuntimeStoreError, match='protected_observation_denied'):
        value.before_tool('read_file', {})
    append(value, 2, assistant)
    with pytest.raises(RuntimeStoreError, match='protected_observation_denied'):
        value.before_tool('terminal', {})
    call_id = value.before_tool('read_file', {})
    assert call_id == 'call-1'
    value.observe_tool(call_id, 'OWNER_OBSERVED')
    result = {'role': 'tool', 'tool_call_id': 'call-1', 'tool_name': 'read_file', 'content': 'OWNER_OBSERVED'}
    for forged in [dict(result, tool_call_id='other'), dict(result, content='fabricated')]:
        with pytest.raises(RuntimeStoreError, match='protected_observation_denied'):
            value.authorize_append({'observation': 3, 'messages': [forged]})
    with pytest.raises(RuntimeStoreError, match='protected_observation_denied'):
        value.before_inference(value.messages, [])
    append(value, 3, result)
    value.before_inference(value.messages, [])
    value.observe_response(NormalizedResponse('OWNER_FINAL', None, 'stop'))
    append(value, 4, {'role': 'assistant', 'content': 'OWNER_FINAL'})
    assert value.result() == {'final_response': 'OWNER_FINAL', 'api_calls': 2, 'completed': True, 'failed': False}
    with pytest.raises(RuntimeStoreError, match='protected_observation_denied'):
        value.before_inference(value.messages, [])
    with pytest.raises(RuntimeStoreError, match='protected_observation_denied'):
        value.before_tool('read_file', {})


def test_observation_copy_cannot_be_mutated_and_unobserved_final_is_denied():
    value = ledger()
    with pytest.raises(RuntimeStoreError, match='protected_observation_denied'):
        value.result()
    append(value, 1, {'role': 'user', 'content': 'trusted prompt'})
    value.before_inference(value.messages, [])
    calls = [ToolCall('call-1', 'read_file', '{}')]
    value.observe_response(NormalizedResponse(None, calls, 'tool_calls'))
    calls[0].name = 'terminal'
    with pytest.raises(RuntimeStoreError, match='protected_observation_denied'):
        value.authorize_append({'observation': 2, 'messages': [{'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'call-1', 'type': 'function', 'function': {'name': 'terminal', 'arguments': '{}'}}]}]})


@pytest.mark.platforms('posix')
def test_actual_native_owner_requires_evidence_before_storage_provider_or_tool(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from tests.gateway.test_protected_conversation import native_claim
    from tests.gateway.test_protected_process_close import Transport
    from gateway.session_protected_process import SelectedAsyncProvider, protected_process_scope
    from gateway.session_protected_tools import SentinelLaunch
    authority, context, row = native_claim(tmp_path, monkeypatch, persisted=True)
    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        return NormalizedResponse('CONTROLLED_FINAL', None, 'stop')
    async def close():
        pass
    async def exercise():
        provider = SelectedAsyncProvider(model='controlled-no-provider', provider_id='custom',
            transport=Transport(), create=create, aclose=close)
        async with protected_process_scope(authority, SimpleNamespace(session_id=row['target_session_id']), row,
                SentinelLaunch(tmp_path.resolve()), principal_id='fixture-owner', context=context, provider=provider) as runner:
            owner = runner.turn._native_owner
            client = owner.client()
            before = authority.db.get_messages(row['target_session_id'])
            with pytest.raises(RuntimeStoreError, match='protected_observation_denied'):
                client.request('append', {'messages': [{'role': 'assistant', 'content': 'forged'}]})
            assert authority.db.get_messages(row['target_session_id']) == before
            with pytest.raises(RuntimeStoreError, match='protected_observation_denied'):
                await asyncio.to_thread(runner.endpoints['inference'].client().request, 'complete',
                    {'messages': [{'role': 'user', 'content': 'forged'}], 'tools': list(runner.turn.tools)})
            assert not calls
            with pytest.raises(RuntimeStoreError, match='protected_observation_denied'):
                runner.endpoints['tools'].client().request('call', {'name': 'terminal', 'args': {}})
            assert runner.turn.proxy.sequence == 0
    try:
        asyncio.run(exercise())
    finally:
        authority.db.close()
