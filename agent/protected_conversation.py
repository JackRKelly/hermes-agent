"""Restricted conversation/tool route. No config, hooks, registry or provider discovery.

This is an explicit, bounded subset, not all AIAgent features. Provider dialects
must enter through Hermes' NormalizedResponse contract. The fresh process facade
uses authenticated bounded IPC; legacy routing fixtures also use in-process
capabilities. Neither path enables ordinary managed production admission.
"""
import json
from functools import wraps
import inspect
import time

from agent.transports.types import NormalizedResponse, ToolCall

MAX_BYTES = 65536
TOOL_NAMES = ('read_file', 'terminal')


def tool_arguments(name, raw, docker=False):
    """Closed SOURCE-only Docker tool subset; no ordinary host registry."""
    if name not in TOOL_NAMES or type(raw) is not str or len(raw.encode()) > 4096:
        raise ValueError('unsupported protected tool call')
    def unique(items):
        value = dict(items)
        if len(value) != len(items):
            raise ValueError('duplicate tool argument')
        return value
    args = json.loads(raw, object_pairs_hook=unique)
    if type(args) is not dict:
        raise ValueError('invalid protected arguments')
    if not args:
        return args
    key = 'path' if name == 'read_file' else 'command'
    if (not docker or set(args) != {key} or type(args[key]) is not str
            or not 1 <= len(args[key].encode()) <= 2048 or '\x00' in args[key]):
        raise ValueError('unsupported protected arguments')
    return args


def protected_turn_of(agent):
    """Only an explicit, exact instance-owned turn selects this private route."""
    value = getattr(agent, '__dict__', {}).get('_protected_turn')
    if value is not None and type(value) is not ProtectedTurn:
        raise RuntimeError('protected dispatch must use the frozen proxy batch entry')
    return value


def protected_turn_entry(ordinary):
    """Intercept the actual turn entry BEFORE any ordinary feature is evaluated."""
    signature = inspect.signature(ordinary)

    @wraps(ordinary)
    def enter(agent, user_message, *args, **kwargs):
        protected = protected_turn_of(agent)
        if protected is None:
            return ordinary(agent, user_message, *args, **kwargs)
        bound = signature.bind(agent, user_message, *args, **kwargs)
        allowed = {next(iter(signature.parameters)), 'user_message'}
        if any(value is not None for key, value in bound.arguments.items() if key not in allowed):
            raise ValueError('unsupported protected conversation option')
        return protected.run(user_message, lambda response, rows: agent._execute_tool_calls(response, rows, ''))

    return enter


def bounded_copy(value):
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode()
    if len(raw) > MAX_BYTES:
        raise ValueError('protected payload exceeds bound')
    return json.loads(raw)


class CapabilityClient:
    """Source interface; the callback is trusted transport code, never model input."""
    def __init__(self, send, token, binding, channel):
        self._send, self._token = send, token
        self.binding, self.channel = bounded_copy(binding), channel
        self._sequence = 0

    def request(self, operation, payload):
        self._sequence += 1
        return self._send(bounded_copy({'channel': self.channel, 'token': self._token,
            'binding': self.binding, 'sequence': self._sequence, 'operation': operation, 'payload': payload}))


class ProtectedTurn:
    """No auxiliary models, streaming transforms, fallbacks, memory, skills or hooks.

    The lifecycle owner persists exact rows before tools and rechecks the native
    assignment before/after inference and every tool. This doesn't atomically
    couple the two SQLite databases or prove in-flight kernel cancellation.
    """
    def __init__(self, inference, lifecycle, proxy):
        if (json.dumps(inference.binding, sort_keys=True) != json.dumps(lifecycle.binding, sort_keys=True)
                or inference.channel != 'inference' or lifecycle.channel != 'lifecycle'):
            raise ValueError('protected capability binding mismatch')
        self.inference, self.lifecycle, self.proxy = inference, lifecycle, proxy
        self._used = False
        self.deadline = time.monotonic() + 5
        self.tools = tuple({'type': 'function', 'function': {'name': name,
            'description': 'Fixed nonsecret source sentinel only',
            'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}} for name in TOOL_NAMES)
        self.docker = 'docker_tool_v1' in lifecycle.binding
        if self.docker:
            self.tools = tuple({'type': 'function', 'function': {'name': name,
                'description': 'Credentialless Docker-only native tool',
                'parameters': {'type': 'object', 'properties': {
                    'path' if name == 'read_file' else 'command': {'type': 'string'}},
                    'additionalProperties': False}}} for name in TOOL_NAMES)
        self.calls = 0
        self._call_ids = set()
        self._observation = 0

    def _check(self):
        if self.proxy.worker.stop.is_set() or time.monotonic() >= self.deadline:
            raise RuntimeError('protected turn cancelled or expired')
        self.lifecycle.request('check', {})

    def _append(self, rows):
        self._check()
        payload = {'messages': bounded_copy(rows)}
        if self.lifecycle.binding.get('owner_observations_v1') is True:
            self._observation += 1
            payload['observation'] = self._observation
        self.lifecycle.request('append', payload)

    def dispatch(self, assistant, messages):
        """Shared by the real AIAgent batch entry; never inline/registry fallback."""
        calls = assistant.tool_calls
        if type(calls) is not list or not 1 <= len(calls) <= 4 or self.calls + len(calls) > 16:
            raise ValueError('protected tool budget exceeded')
        parsed, seen = [], set()
        for tc in calls:
            if (type(tc) is not ToolCall or type(tc.id) is not str or not 1 <= len(tc.id) <= 128
                    or tc.id in seen or tc.id in self._call_ids
                    or type(tc.name) is not str or tc.name not in TOOL_NAMES
                    or tc.provider_data or type(tc.arguments) is not str):
                raise ValueError('unsupported protected tool call')
            tool_arguments(tc.name, tc.arguments, self.docker)
            seen.add(tc.id)
            parsed.append(tc)
        # Validate the ENTIRE batch before side effects or publishing its call row.
        self._call_ids.update(seen)
        row = {'role': 'assistant', 'content': assistant.content or '', 'tool_calls': [
            {'id': tc.id, 'type': 'function', 'function': {'name': tc.name, 'arguments': json.dumps(tool_arguments(tc.name, tc.arguments, self.docker), separators=(',', ':'))}} for tc in parsed]}
        self._append([row])
        messages.append(row)
        for tc in parsed:
            self._check()
            self.calls += 1
            result = self.proxy.invoke(tc.name, tool_arguments(tc.name, tc.arguments, self.docker))
            self._check()  # refuse stale/in-flight results, not just stale requests
            row = {'role': 'tool', 'tool_call_id': tc.id, 'tool_name': tc.name, 'content': result}
            self._append([row])
            messages.append(row)

    def run(self, prompt, dispatch=None):
        if self._used:
            raise RuntimeError('protected turn is single use')
        self._used = True
        try:
            if type(prompt) is not str or not prompt or len(prompt.encode()) > 4096:
                raise ValueError('invalid protected prompt')
            messages = [{'role': 'user', 'content': prompt}]
            self._append(messages)
            for count in range(1, 5):
                self._check()
                response = self.inference.request('complete', {'messages': bounded_copy(messages), 'tools': bounded_copy(self.tools)})
                self._check()
                if (type(response) is not NormalizedResponse or response.provider_data or response.reasoning
                        or response.finish_reason not in ('stop', 'tool_calls')
                        or response.content is not None and (type(response.content) is not str or len(response.content.encode()) > 4096)):
                    raise ValueError('unsupported protected inference response')
                if response.tool_calls:
                    if response.finish_reason != 'tool_calls':
                        raise ValueError('inconsistent protected response')
                    (dispatch or self.dispatch)(response, messages)
                elif response.finish_reason == 'stop' and response.content:
                    self._append([{'role': 'assistant', 'content': response.content}])
                    return {'final_response': response.content, 'api_calls': count, 'completed': True, 'failed': False}
                else:
                    raise ValueError('incomplete protected response')
            raise RuntimeError('protected inference budget exceeded')
        except BaseException:
            self.proxy.worker.close()
            raise
