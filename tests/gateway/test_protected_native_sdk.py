"""Actual async SDKs against a loopback fixture; NO external provider/auth."""
import asyncio
import json

import pytest


def test_sdk_module_import_is_discovery_free():
    import subprocess
    import sys
    source = '''
import sys
class Fence:
 def find_spec(self, name, *args):
  if name in {'hermes_cli.auth', 'hermes_cli.runtime_provider', 'providers', 'model_tools', 'run_agent', 'agent.auxiliary_client', 'tools.registry'}:
   raise RuntimeError('discovery/import reached: ' + name)
sys.meta_path.insert(0, Fence())
import gateway.session_protected_native_sdk
'''
    result = subprocess.run([sys.executable, '-c', source], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_strict_unknown_accounting_denies_before_sdk_construction():
    from gateway.session_protected_native_sdk import NativeSDKPolicy, construct_native_sdk
    policy = NativeSDKPolicy(provider='openai-codex', model='gpt-6.1-sol', session_id='controlled',
        base_url='https://chatgpt.com/backend-api/codex', accounting='strict')
    def forbidden():
        pytest.fail('credentials resolved before strict accounting gate')
    with pytest.raises(RuntimeError, match='strict_budget_unproven'):
        construct_native_sdk(policy, resolve=forbidden, ledger=None)


async def serve_fixture(reader, writer, *, state, provider, delay):
    """Tiny actual HTTP peer, with dummy responses clearly marked controlled."""
    try:
        header = await reader.readuntil(b'\r\n\r\n')
        size = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:'))
        body = json.loads(await reader.readexactly(size))
        state['requests'].append((header, body))
        state['received'].set()
        if delay == 'headers':
            assert await reader.read() == b''
            state['disconnected'].set()
            return
        response = (dict(id='controlled-message', type='message', role='assistant', model=body['model'],
            content=[dict(type='text', text='CONTROLLED_SDK_DONE')], stop_reason='end_turn',
            usage=dict(input_tokens=3, output_tokens=2)) if provider == 'anthropic' else
            dict(id='controlled-response', object='response', created_at=0, status='completed', model=body['model'],
                 output=[dict(type='message', id='controlled-msg', role='assistant', status='completed',
                              content=[dict(type='output_text', text='CONTROLLED_SDK_DONE', annotations=[])])],
                 usage=dict(input_tokens=3, output_tokens=2, total_tokens=5)))
        if state.get('tool_roundtrip') and len(state['requests']) == 1:
            if provider == 'anthropic':
                response['content'] = [dict(type='tool_use', id='controlled-read',
                    name=state.get('tool_name', body['tools'][0]['name']), input={})]
                if state.get('signed_thinking'):
                    response['content'].insert(0, dict(type='thinking', thinking='CONTROLLED_OWNER_THINKING',
                        signature='CONTROLLED_SIGNED_THINKING'))
                response['stop_reason'] = 'tool_use'
            else:
                response['output'] = [dict(type='function_call', id='fc-controlled-read', call_id='controlled-read',
                    name='read_file', arguments='{}', status='completed')]
        elif state.get('tool_roundtrip'):
            assert 'CONTROLLED_ARTIFACT' in json.dumps(body), 'real Docker tool result must reach SDK follow-up'
            if state.get('signed_thinking'):
                assistant = next(m for m in body['messages'] if m['role'] == 'assistant')
                assert [b['type'] for b in assistant['content']] == ['thinking', 'tool_use']
                assert assistant['content'][0]['signature'] == 'CONTROLLED_SIGNED_THINKING'
                assert assistant['content'][1]['name'] == state['requests'][0][1]['tools'][0]['name']
        fault = state.get('fault')
        if fault == 'model': response['model'] = 'CONTROLLED_WRONG_MODEL'  # health: allow HX011 -- explicit controlled HTTP fault vectors, not runtime routing
        elif fault == 'usage': response.pop('usage')
        elif fault == 'oversize': response['controlled_padding'] = 'X' * 70000
        elif fault == 'status':
            payload = json.dumps(dict(error=dict(type='api_error', message='CONTROLLED_DUMMY_NOT_A_SECRET'))).encode()
            writer.write(f'HTTP/1.1 500 Error\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n'.encode() + payload)
            await writer.drain()
            return
        if provider == 'openai-codex':
            payload = ('event: response.completed\ndata: ' + json.dumps(dict(type='response.completed', response=response)) + '\n\n').encode()
            content_type = 'text/event-stream'
        else:
            payload = json.dumps(response).encode()
            content_type = 'application/json'
        writer.write(f'HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n'.encode())
        await writer.drain()
        if delay == 'body':
            writer.write(payload[:1])
            await writer.drain()
            assert await reader.read() == b''
            state['disconnected'].set()
            return
        writer.write(payload)
        await writer.drain()
    except ConnectionResetError:
        if delay not in ('headers', 'body'):
            raise
        state['disconnected'].set()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionResetError:
            # A peer reset is also a real closed connection after cancellation.
            if delay not in ('headers', 'body'):
                raise
            state['disconnected'].set()


class ControlledLedger:
    """Fixture only. Production supplies the signed broker's durable ledger."""
    def __init__(self):
        self.reserved, self.usages, self.frozen = [], [], []
    def reserve(self, binding):
        self.reserved.append(binding)
        return len(self.reserved)
    def reconcile(self, receipt, usage):
        self.usages.append((receipt, usage))
    def freeze(self, receipt, reason):
        self.frozen.append((receipt, reason))


@pytest.mark.parametrize('provider', ['anthropic', 'openai-codex'])
@pytest.mark.parametrize('delay', [None, 'headers', 'body', 'timeout'])
def test_actual_sdk_complete_or_cancel_closes_peer(provider, delay):
    from gateway.session_protected_native_sdk import NativeSDKPolicy, construct_native_sdk
    async def run():
        state = dict(requests=[], received=asyncio.Event(), disconnected=asyncio.Event())
        peers = set()
        def connected(reader, writer):
            task = asyncio.create_task(serve_fixture(reader, writer, state=state, provider=provider,
                delay='headers' if delay == 'timeout' else delay))
            peers.add(task)
        server = await asyncio.start_server(connected, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        ledger = ControlledLedger()
        policy = NativeSDKPolicy(provider=provider, model='controlled-model', session_id='controlled-session',
            base_url=f'http://127.0.0.1:{port}', accounting='subscription_request_bound',
            request_seconds=2 if delay == 'timeout' else 5)
        # Trusted native owner normally resolves the snapshot; never guest/row JSON.
        selected = construct_native_sdk(policy, resolve=lambda: dict(api_key='CONTROLLED_DUMMY_NOT_A_SECRET',
            provider=provider, base_url=policy.base_url, api_mode='anthropic_messages' if provider == 'anthropic' else 'codex_responses'), ledger=ledger)
        task = asyncio.create_task(selected.complete([{'role': 'user', 'content': 'CONTROLLED_ONLY'}], []))
        try:
            await asyncio.wait_for(state['received'].wait(), 3)
            if delay == 'timeout':
                with pytest.raises(RuntimeError, match='request_failed'):
                    await task
                await asyncio.wait_for(state['disconnected'].wait(), 2)
                assert ledger.frozen and not ledger.usages
            elif delay:
                await selected.close()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await asyncio.wait_for(state['disconnected'].wait(), 2)
                assert ledger.frozen and not ledger.usages
            else:
                result = await asyncio.wait_for(task, 3)
                assert result.content == 'CONTROLLED_SDK_DONE'
                assert result.usage.prompt_tokens == 3 and result.usage.completion_tokens == 2
                assert len(ledger.usages) == 1 and not ledger.frozen
            assert len(ledger.reserved) == len(state['requests']) == 1
            assert state['requests'][0][1]['model'] == 'controlled-model'
            assert 'CONTROLLED_DUMMY' not in json.dumps(state['requests'][0][1])
            if provider == 'openai-codex':
                assert state['requests'][0][1]['stream'] is True
                assert 'max_output_tokens' not in state['requests'][0][1]  # no invented subscription hard cap
                assert b'session_id: controlled-session' in state['requests'][0][0].lower()
            else:
                assert state['requests'][0][1]['max_tokens'] == 1024  # preference, not a proved billing cap
        finally:
            await selected.close()
            server.close()
            await server.wait_closed()
            await asyncio.wait_for(asyncio.gather(*peers), 2)
        assert not selected._tasks and selected._sdk_close_task.done()
    asyncio.run(run())


@pytest.mark.parametrize('provider', ['anthropic', 'openai-codex'])
@pytest.mark.parametrize('fault', ['status', 'model', 'usage', 'oversize'])
def test_real_sdk_fault_freezes_without_retry_or_credential_error_payload(provider, fault):
    from gateway.session_protected_native_sdk import NativeSDKPolicy, construct_native_sdk
    async def run():
        state = dict(requests=[], received=asyncio.Event(), disconnected=asyncio.Event(), fault=fault)
        peers = set()
        def connected(reader, writer):
            peers.add(asyncio.create_task(serve_fixture(reader, writer, state=state, provider=provider, delay=None)))
        server = await asyncio.start_server(connected, '127.0.0.1', 0)
        ledger = ControlledLedger()
        policy = NativeSDKPolicy(provider=provider, model='controlled-model', session_id='controlled',
            base_url=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}', accounting='subscription_request_bound')
        selected = construct_native_sdk(policy, resolve=lambda: dict(provider=provider, base_url=policy.base_url,
            api_mode='anthropic_messages' if provider == 'anthropic' else 'codex_responses',
            api_key='CONTROLLED_DUMMY_NOT_A_SECRET'), ledger=ledger)
        try:
            with pytest.raises(RuntimeError) as failure:
                await selected.complete([dict(role='user', content='CONTROLLED')], [])
            assert 'CONTROLLED_DUMMY' not in str(failure.value)
            assert len(ledger.reserved) == len(state['requests']) == 1
            assert ledger.frozen and not ledger.usages
            with pytest.raises(RuntimeError, match='frozen'):
                await selected.complete([dict(role='user', content='NO_RETRY')], [])
            assert len(state['requests']) == 1
        finally:
            await selected.close()
            server.close()
            await server.wait_closed()
            await asyncio.wait_for(asyncio.gather(*peers), 2)
    asyncio.run(run())
