"""Native endpoint/header branches over actual loopback HTTP, dummy credentials only.

Only the lower socket destination is redirected by this explicit fixture. The
real SDK, native host/endpoint checks, auth classification, headers, transports,
serialized request and response parser execute unchanged. No external auth/read.
"""
import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from tests.gateway.test_protected_native_sdk import ControlledLedger, serve_fixture


@pytest.mark.parametrize('kind', ['anthropic_api', 'anthropic_oauth', 'codex_oauth'])
def test_native_identity_headers_remain_owner_only_on_real_sdk_request(monkeypatch, kind):
    import httpx
    from gateway.session_protected_native_sdk import NativeSDKPolicy, construct_native_sdk
    from agent import anthropic_adapter
    from hermes_cli import version_info
    # Version-only leaf seam, never native auth/keychain/credential discovery.
    monkeypatch.setattr(anthropic_adapter, '_get_claude_code_version', lambda: 'CONTROLLED')
    monkeypatch.setattr(version_info, 'get_version_info', lambda: SimpleNamespace(base_version='CONTROLLED'))
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'CONTROLLED_OTHER_API_KEY')
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'CONTROLLED_OTHER_BEARER')
    provider = 'openai-codex' if kind == 'codex_oauth' else 'anthropic'
    endpoint = 'https://chatgpt.com/backend-api/codex' if provider == 'openai-codex' else 'https://api.anthropic.com'
    if kind == 'codex_oauth':
        claims = {'https://api.openai.com/auth': {'chatgpt_account_id': 'CONTROLLED_ACCOUNT'}}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        key = 'CONTROLLED.' + payload + '.DUMMY'
    else:
        key = 'sk-ant-oat-CONTROLLED_DUMMY' if kind == 'anthropic_oauth' else 'sk-ant-api-CONTROLLED_DUMMY'
    async def run():
        state = dict(requests=[], received=asyncio.Event(), disconnected=asyncio.Event())
        peers = set()
        def connected(reader, writer):
            peers.add(asyncio.create_task(serve_fixture(reader, writer, state=state, provider=provider, delay=None)))
        server = await asyncio.start_server(connected, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        real_http = httpx.AsyncHTTPTransport.handle_async_request
        async def loopback_only(transport, request):
            assert str(request.url).startswith(endpoint)
            request.url = httpx.URL(f'http://127.0.0.1:{port}' + request.url.path)
            return await real_http(transport, request)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', loopback_only)
        policy = NativeSDKPolicy(provider=provider, model='controlled-model', session_id='controlled-native-session',
            base_url=endpoint, accounting='subscription_request_bound')
        ledger = ControlledLedger()
        selected = construct_native_sdk(policy, ledger=ledger, resolve=lambda: dict(provider=provider,
            base_url=endpoint, api_key=key, api_mode='codex_responses' if provider == 'openai-codex' else 'anthropic_messages'))
        try:
            response = await selected.complete([dict(role='user', content='CONTROLLED_NATIVE_HEADERS')], [])
            assert response.content == 'CONTROLLED_SDK_DONE'
            assert len(state['requests']) == 1
            header, body = state['requests'][0]
            assert key not in json.dumps(body)
            assert b'CONTROLLED_OTHER' not in header
            lower = header.lower()
            if kind == 'anthropic_oauth':
                assert ('authorization: Bearer ' + key).encode().lower() in lower
                assert b'x-api-key:' not in lower and b'user-agent: claude-code/controlled' in lower
                assert b'anthropic-beta:' in lower and b'x-app: cli' in lower
            elif kind == 'anthropic_api':
                assert ('x-api-key: ' + key).encode().lower() in lower
                assert b'authorization:' not in lower
            else:
                assert b'chatgpt-account-id: controlled_account' in lower
                assert b'originator: hermes-agent' in lower
                assert b'session_id: controlled-native-session' in lower
            assert len(ledger.reserved) == len(ledger.usages) == 1
        finally:
            await selected.close()
            server.close()
            await server.wait_closed()
            await asyncio.wait_for(asyncio.gather(*peers), 2)
    asyncio.run(run())


def _exercise_native_tool_roundtrip(monkeypatch, kind, *, bad_tool=False):
    import httpx
    from gateway.session_protected_native_sdk import NativeSDKPolicy, construct_native_sdk
    from agent import anthropic_adapter
    from hermes_cli import version_info
    monkeypatch.setattr(anthropic_adapter, '_get_claude_code_version', lambda: 'CONTROLLED')
    monkeypatch.setattr(version_info, 'get_version_info', lambda: SimpleNamespace(base_version='CONTROLLED'))
    monkeypatch.setenv('OPENAI_ORG_ID', 'CONTROLLED_AMBIENT_ORGANIZATION')
    monkeypatch.setenv('OPENAI_PROJECT_ID', 'CONTROLLED_AMBIENT_PROJECT')
    provider = 'openai-codex' if kind == 'codex_oauth' else 'anthropic'
    endpoint = 'https://chatgpt.com/backend-api/codex' if provider == 'openai-codex' else 'https://api.anthropic.com'
    key = 'sk-ant-' + ('oat01-' if kind == 'anthropic_oauth' else 'api03-') + 'CONTROLLED_NOT_A_SECRET'
    if provider == 'openai-codex':
        key = 'CONTROLLED.DUMMY.DUMMY'
    async def run():
        state = dict(requests=[], received=asyncio.Event(), disconnected=asyncio.Event(),
            tool_roundtrip=True, signed_thinking=provider == 'anthropic')
        if bad_tool:
            state['tool_name'] = 'mcp__delegate_task'
        peers = set()
        def connected(reader, writer):
            peers.add(asyncio.create_task(serve_fixture(reader, writer, state=state, provider=provider, delay=None)))
        server = await asyncio.start_server(connected, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        real_http = httpx.AsyncHTTPTransport.handle_async_request
        async def loopback_only(transport, request):
            assert str(request.url).startswith(endpoint)
            request.url = httpx.URL(f'http://127.0.0.1:{port}' + request.url.path)
            return await real_http(transport, request)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', loopback_only)
        policy = NativeSDKPolicy(provider=provider,
            model='claude-opus-5-5' if provider == 'anthropic' else 'gpt-6.1-sol',
            session_id='CONTROLLED_TOOL_SESSION', base_url=endpoint, accounting='subscription_request_bound')
        ledger = ControlledLedger()
        selected = construct_native_sdk(policy, ledger=ledger, resolve=lambda: dict(provider=provider,
            base_url=endpoint, api_key=key, api_mode='codex_responses' if provider == 'openai-codex' else 'anthropic_messages'))
        tools = [dict(type='function', function=dict(name='read_file', description='CONTROLLED local tool',
            parameters=dict(type='object', properties={}, additionalProperties=False)))]
        messages = [dict(role='user', content='CONTROLLED_NATIVE_TOOL_ROUNDTRIP')]
        try:
            if bad_tool:
                with pytest.raises(RuntimeError, match='protected_sdk_request_failed'):
                    await selected.complete(messages, tools)
                assert len(ledger.reserved) == len(ledger.frozen) == 1 and not ledger.usages
                return
            response = await selected.complete(messages, tools)
            assert response.tool_calls[0].name == 'read_file'
            assert response.provider_data is None and response.reasoning is None
            assert 'CONTROLLED_OWNER_THINKING' not in repr(response)
            call = response.tool_calls[0]
            messages.extend([dict(role='assistant', content=response.content or '', tool_calls=[dict(
                id=call.id, type='function', function=dict(name=call.name, arguments=call.arguments))]),
                dict(role='tool', tool_call_id=call.id, content='CONTROLLED_ARTIFACT')])
            final = await selected.complete(messages, tools)
            assert final.content == 'CONTROLLED_SDK_DONE'
            assert len(state['requests']) == len(ledger.reserved) == len(ledger.usages) == 2
            for header, body in state['requests']:
                assert b'CONTROLLED_AMBIENT' not in header
                assert key not in json.dumps(body)
        finally:
            await selected.close()
            server.close()
            await server.wait_closed()
            await asyncio.wait_for(asyncio.gather(*peers), 2)
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['anthropic_api', 'anthropic_oauth', 'codex_oauth'])
def test_native_tools_and_signed_thinking_remain_owner_only(monkeypatch, kind):
    _exercise_native_tool_roundtrip(monkeypatch, kind)


def test_oauth_unknown_wire_tool_denies_before_followup(monkeypatch):
    _exercise_native_tool_roundtrip(monkeypatch, 'anthropic_oauth', bad_tool=True)
