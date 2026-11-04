"""Owner-only native async SDK construction. No auth or plugin discovery at import.

The native owner supplies its resolved auth/headers snapshot behind ``resolve``;
no credentials, endpoint selection or request overrides cross guest IPC. This
module does NOT verify operator signatures: the admission bridge does, through
the deployed broker verifier, before constructing this capability. Direct Python
construction is trusted owner code, not an untrusted admission API.
"""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
from urllib.parse import urlsplit

from gateway.session_protected_provider import SelectedAsyncProvider
from gateway.session_protected_timing import PilotTiming


@dataclass(frozen=True)
class NativeSDKPolicy:
    provider: str
    model: str
    session_id: str
    base_url: str
    accounting: str = 'strict'
    max_output_tokens: int = 1024
    request_seconds: float = 5.0
    response_bytes: int = 65536
    pilot_timing: PilotTiming | None = None

    def validate(self):
        if self.accounting == 'strict':
            # Neither native subscription backend currently proves strict USD/token
            # enforcement. max_tokens is a request preference, NOT such proof.
            raise RuntimeError('strict_budget_unproven')
        if self.accounting != 'subscription_request_bound':
            raise RuntimeError('protected_accounting_policy_denied')
        ceiling = 5
        if self.pilot_timing is not None:
            if type(self.pilot_timing) is not PilotTiming:
                raise RuntimeError('protected_pilot_timing_denied')
            self.pilot_timing.validate()
            ceiling = self.pilot_timing.request_seconds
        if (self.provider not in ('openai-codex', 'anthropic') or not self.model or not self.session_id
                or type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 1024
                or type(self.request_seconds) not in (int, float) or not 0 < self.request_seconds <= ceiling
                or type(self.response_bytes) is not int or not 1024 <= self.response_bytes <= 65536):
            raise RuntimeError('protected_sdk_policy_denied')
        endpoint = urlsplit(self.base_url)
        local = endpoint.scheme == 'http' and endpoint.hostname == '127.0.0.1'
        native = endpoint.scheme == 'https' and (
            (self.provider == 'anthropic' and endpoint.netloc == 'api.anthropic.com' and endpoint.path in ('', '/'))
            or (self.provider == 'openai-codex' and endpoint.netloc == 'chatgpt.com' and endpoint.path == '/backend-api/codex'))
        if not (local or native) or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise RuntimeError('protected_sdk_endpoint_denied')


async def _completed_response(stream):
    response = None
    async with stream:
        async for event in stream:
            if event.type == 'response.completed':
                if response is not None:
                    raise RuntimeError('protected_sdk_duplicate_completion')
                response = event.response
    return response


def construct_native_sdk(policy, *, resolve, ledger):
    """Called only after trusted admission; ledger reserves atomically before POST.

    ``resolve`` is the owner-native pinned auth/session/header resolver, never
    supplied by model JSON. ``ledger.reserve(binding)`` must reverify signed
    approval, durable <=12 cumulative requests and <=600s from controller start;
    cancelled/failed reservations are NEVER refunded. No SDK retry or fallback.
    """
    if type(policy) is not NativeSDKPolicy:
        raise RuntimeError('protected_sdk_policy_denied')
    policy.validate()  # BEFORE credentials, SDK construction, or any network
    if policy.pilot_timing is not None and not callable(getattr(ledger, 'remaining_seconds', None)):
        raise RuntimeError('protected_pilot_deadline_required')
    if ledger is None or not all(callable(getattr(ledger, k, None)) for k in ('reserve', 'reconcile', 'freeze')):
        raise RuntimeError('protected_request_ledger_required')
    try:
        runtime = resolve()
    except Exception:  # health: allow BLE001 -- sanitize native auth failures; never expose credential-bearing exception bodies
        raise RuntimeError('protected_native_auth_unavailable') from None
    expected_mode = 'anthropic_messages' if policy.provider == 'anthropic' else 'codex_responses'
    if (type(runtime) is not dict or runtime.get('provider') != policy.provider
            or runtime.get('base_url') != policy.base_url or runtime.get('api_mode') != expected_mode
            or type(runtime.get('api_key')) is not str or not runtime['api_key']):
        raise RuntimeError('protected_native_route_mismatch')
    import httpx

    class LimitedStream(httpx.AsyncByteStream):
        def __init__(self, stream):
            self.stream = stream
        async def __aiter__(self):
            size = 0
            async for chunk in self.stream:
                size += len(chunk)
                if size > policy.response_bytes:
                    raise RuntimeError('protected_sdk_body_limit')
                yield chunk
        async def aclose(self):
            await self.stream.aclose()

    class LimitedTransport(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            # SDK extras cannot change authority or add redirects/retries. Body
            # and error responses have the same hard read bound.
            endpoint = urlsplit(str(request.url))
            selected = urlsplit(policy.base_url)
            if endpoint.netloc != selected.netloc or endpoint.scheme != selected.scheme or request.method != 'POST':
                raise RuntimeError('protected_sdk_endpoint_denied')
            response = await super().handle_async_request(request)
            encoding = response.headers.get('content-encoding', 'identity')
            if encoding != 'identity':
                await response.aclose()
                raise RuntimeError('protected_sdk_encoding_denied')
            response.stream = LimitedStream(response.stream)
            return response

    http = httpx.AsyncClient(transport=LimitedTransport(retries=0, trust_env=False), trust_env=False,
        follow_redirects=False, timeout=httpx.Timeout(policy.request_seconds), headers={'Accept-Encoding': 'identity'})
    headers = dict(runtime.get('default_headers') or {})
    if policy.provider == 'anthropic':
        import anthropic
        from agent.anthropic_adapter import (
            _beta_header, _common_betas_for_base_url,
            _get_claude_code_version, _OAUTH_ONLY_BETAS,
        )
        # Reuse native endpoint/token classification and header resolution, not
        # the auxiliary router or a sync-client-to-thread wrapper. No SDK install,
        # plugin lookup, credential discovery or provider fallback occurs here.
        from agent.anthropic_credentials import anthropic_route_is_oauth
        oauth = anthropic_route_is_oauth(policy.base_url, runtime['api_key'], provider=policy.provider)
        betas = _common_betas_for_base_url(policy.base_url)
        native_headers = _beta_header(betas + _OAUTH_ONLY_BETAS if oauth else betas)
        if oauth:
            native_headers.update({'user-agent': f'claude-code/{_get_claude_code_version()} (external, cli)', 'x-app': 'cli'})
        headers = {**native_headers, **headers}
        headers['X-Api-Key' if oauth else 'Authorization'] = anthropic.Omit()
        sdk = anthropic.AsyncAnthropic(**{'auth_token' if oauth else 'api_key': runtime['api_key']},
            base_url=policy.base_url, default_headers=headers, http_client=http, max_retries=0)
        from agent.transports.anthropic import AnthropicTransport
        native_transport = AnthropicTransport()
        create = sdk.messages.create
    else:
        from openai import AsyncOpenAI, Omit
        from agent.codex_headers import codex_cloudflare_headers, apply_required_codex_headers
        headers = {**codex_cloudflare_headers(runtime['api_key'], base_url=policy.base_url), **headers}
        required = {'default_headers': headers}
        apply_required_codex_headers(required, access_token=runtime['api_key'], base_url=policy.base_url)
        headers = {**required['default_headers'], 'OpenAI-Organization': Omit(), 'OpenAI-Project': Omit()}
        sdk = AsyncOpenAI(api_key=runtime['api_key'], base_url=policy.base_url,
            default_headers=headers, http_client=http, max_retries=0)
        from agent.transports.codex import ResponsesApiTransport
        native_transport = ResponsesApiTransport()
        create = sdk.responses.create
        oauth = False
    frozen = False
    active = False
    # Signed thinking is an opaque owner-only replay channel, never guest state.
    # At most twelve bounded SDK responses can populate it under the signed ledger.
    owner_replay = {}

    class PinnedTransport:
        def build_kwargs(self, model, messages, tools):
            if model != policy.model:
                raise RuntimeError('protected_sdk_model_mismatch')
            from agent.codex_responses_adapter import ResponsesEndpointQuirks
            kwargs = native_transport.build_kwargs(model, messages, tools,
                endpoint=ResponsesEndpointQuirks(),
                base_url=policy.base_url, provider=policy.provider,
                session_id=policy.session_id, cache_scope_id=policy.session_id,
                is_codex_backend=policy.provider == 'openai-codex', is_oauth=oauth, native_endpoint=True,
                max_tokens=policy.max_output_tokens, timeout=policy.request_seconds)
            if kwargs.get('model') != policy.model:
                raise RuntimeError('protected_sdk_model_mismatch')
            if policy.provider == 'anthropic':
                rows = [m for m in messages if m['role'] == 'assistant']
                wire = [m for m in kwargs['messages'] if m['role'] == 'assistant']
                if len(rows) != len(wire):
                    raise RuntimeError('protected_sdk_replay_unbound')
                for row, outgoing in zip(rows, wire):
                    key = tuple(c['id'] for c in row.get('tool_calls', []))
                    saved, blocks = owner_replay.get(key, (None, None))
                    if saved != row:
                        raise RuntimeError('protected_sdk_replay_unbound')
                    if blocks:
                        outgoing['content'] = deepcopy(blocks)
            return kwargs
        def normalize_response(self, response):
            return response

    async def metered_create(**kwargs):
        nonlocal frozen, active
        if frozen or active:
            raise RuntimeError('protected_request_frozen')
        active = True
        receipt = None
        try:
            receipt = ledger.reserve(dict(provider=policy.provider, model=policy.model,
                session_id=policy.session_id, base_url=policy.base_url, accounting=policy.accounting))
            if receipt is None:
                raise RuntimeError('protected_request_reservation_denied')
            if policy.provider == 'openai-codex':
                kwargs['stream'] = True  # native Codex requires the SSE Responses path
            seconds = policy.request_seconds
            if policy.pilot_timing is not None:
                seconds = min(seconds, ledger.remaining_seconds())
                if seconds <= 0:
                    raise RuntimeError('protected_pilot_deadline_expired')
                kwargs['timeout'] = seconds
            async with asyncio.timeout(seconds):
                response = await create(**kwargs)
                if policy.provider == 'openai-codex':
                    response = await _completed_response(response)
                if response is None or getattr(response, 'model', None) != policy.model:
                    raise RuntimeError('protected_sdk_model_mismatch')
                usage = getattr(response, 'usage', None)
                values = [getattr(usage, name, None) for name in ('input_tokens', 'output_tokens')]
                if any(type(v) is not int or v < 0 for v in values):
                    raise RuntimeError('protected_sdk_usage_unknown')
                if policy.provider == 'openai-codex' and getattr(response, 'status', None) != 'completed':
                    raise RuntimeError('protected_sdk_incomplete')
                normalized = native_transport.normalize_response(response, strip_tool_prefix=False,
                    untrusted_completed_issuers=frozenset({'codex_backend', 'xai_responses'}))
                from agent.protected_conversation import TOOL_NAMES, tool_arguments
                from agent.transports.types import NormalizedResponse, ToolCall, Usage
                # Local closed-name mapping: OAuth normalization must NEVER consult
                # tools.registry, including on lean/polluted ordinary host owners.
                calls = []
                for call in normalized.tool_calls or []:
                    name = call.name
                    if oauth:
                        if type(name) is not str or not name.startswith('mcp__'):
                            raise RuntimeError('protected_sdk_tool_denied')
                        name = name[5:]
                    if name not in TOOL_NAMES:
                        raise RuntimeError('protected_sdk_tool_denied')
                    arguments = json.dumps(tool_arguments(name, call.arguments, docker=True), separators=(',', ':'))
                    calls.append(ToolCall(call.call_id or call.id, name, arguments))
                result = NormalizedResponse(normalized.content, calls or None, normalized.finish_reason,
                    usage=Usage(prompt_tokens=values[0], completion_tokens=values[1], total_tokens=sum(values)))
                if policy.provider == 'anthropic' and calls:
                    key = tuple(c.id for c in calls)
                    if key in owner_replay or len(owner_replay) >= 12 or len(set(key)) != len(key):
                        raise RuntimeError('protected_sdk_replay_unbound')
                    row = dict(role='assistant', content=result.content or '', tool_calls=[dict(
                        id=c.id, type='function', function=dict(name=c.name, arguments=c.arguments)) for c in calls])
                    blocks = (normalized.provider_data or {}).get('anthropic_content_blocks')
                    owner_replay[key] = (deepcopy(row), deepcopy(blocks))
                ledger.reconcile(receipt, dict(model=response.model, input_tokens=values[0], output_tokens=values[1]))
                return result
        except BaseException as error:
            frozen = True
            if receipt is not None:
                ledger.freeze(receipt, 'request_cancelled_or_accounting_unknown')
            if isinstance(error, asyncio.CancelledError):
                raise
            # SDK errors can echo arbitrary response bodies (including secrets).
            # Never propagate those into owner diagnostics or guest wire payloads.
            raise RuntimeError('protected_sdk_request_failed') from None
        finally:
            active = False

    async def close_sdk():
        try:
            await sdk.close()
        except asyncio.CancelledError:
            raise
        except Exception:  # health: allow BLE001 -- sanitize arbitrary SDK close faults at the owner boundary without credential-bearing tracebacks
            from gateway.session_protected_provider import ProviderCloseFailed
            raise ProviderCloseFailed() from None
        finally:
            owner_replay.clear()

    return SelectedAsyncProvider(model=policy.model, provider_id=policy.provider,
        transport=PinnedTransport(), create=metered_create, aclose=close_sdk)
