"""Owner-only evidence ledger for the fresh SOURCE conversation, not child trust.

Only owner-observed responses/results authorize rows. Correlations are monotonic
within this exact lifecycle capability; tokens authenticate transport, not truth.
Legacy synchronous routing fixtures do not use this ledger or claim this boundary.
"""
import json
import threading

from agent.protected_conversation import TOOL_NAMES, bounded_copy, tool_arguments
from agent.transports.types import NormalizedResponse, ToolCall
from hermes_state_runtime import RuntimeStoreError


def _deny():
    raise RuntimeStoreError('protected_observation_denied')


class OwnerObservations:
    def __init__(self, tools):
        self._tools = bounded_copy(tools)
        self._docker = any(t['function']['description'] == 'Credentialless Docker-only native tool' for t in tools)
        self._messages, self._calls = [], []
        self._ids = set()
        self._pending = None
        self._sequence = self._inferences = 0
        self._started = self._inflight = self._finished = False
        self._tool = None
        self._lock = threading.RLock()

    @property
    def messages(self):
        with self._lock:
            return bounded_copy(self._messages)

    def start(self, prompt):
        with self._lock:
            if self._started or type(prompt) is not str or not prompt or len(prompt.encode()) > 4096:
                _deny()
            self._started = True
            self._pending = [{'role': 'user', 'content': prompt}]

    def authorize_append(self, payload):
        with self._lock:
            if (type(payload) is not dict or set(payload) != {'observation', 'messages'}
                    or type(payload['observation']) is not int or payload['observation'] != self._sequence + 1
                    or self._pending is None or payload['messages'] != self._pending):
                _deny()
            return bounded_copy(self._pending)

    def commit_append(self, payload):
        with self._lock:
            rows = self.authorize_append(payload)
            self._messages.extend(rows)
            self._sequence += 1
            self._pending = None

    def before_inference(self, messages, tools):
        with self._lock:
            if (not self._started or self._pending is not None or self._calls or self._tool is not None
                    or self._inflight or self._finished or self._inferences >= 4
                    or messages != self._messages or tools != self._tools):
                _deny()
            self._inflight = True
            self._inferences += 1

    def observe_response(self, response):
        with self._lock:
            if (not self._inflight or type(response) is not NormalizedResponse
                    or response.provider_data or response.reasoning
                    or response.content is not None and (type(response.content) is not str
                        or len(response.content.encode()) > 4096)):
                _deny()
            calls = response.tool_calls
            if calls:
                if (response.finish_reason != 'tool_calls' or type(calls) is not list
                        or not 1 <= len(calls) <= 4 or len(self._ids) + len(calls) > 16):
                    _deny()
                seen = set()
                for call in calls:
                    if (type(call) is not ToolCall or type(call.id) is not str or not 1 <= len(call.id) <= 128
                            or call.id in seen or call.id in self._ids or call.name not in TOOL_NAMES
                            or call.provider_data or type(call.arguments) is not str):
                        _deny()
                    tool_arguments(call.name, call.arguments, self._docker)
                    seen.add(call.id)
                row = {'role': 'assistant', 'content': response.content or '', 'tool_calls': [
                    {'id': call.id, 'type': 'function', 'function': {'name': call.name, 'arguments': json.dumps(tool_arguments(call.name, call.arguments, self._docker), separators=(',', ':'))}}
                    for call in calls]}
                self._calls = [(call.id, call.name, tool_arguments(call.name, call.arguments, self._docker)) for call in calls]
                self._ids.update(seen)
            elif response.finish_reason == 'stop' and response.content:
                row = {'role': 'assistant', 'content': response.content}
                self._finished = True
            else:
                _deny()
            self._pending = bounded_copy([row])
            self._inflight = False

    def before_tool(self, name, args):
        with self._lock:
            if (self._pending is not None or self._inflight or self._tool is not None
                    or not self._calls or name != self._calls[0][1] or args != self._calls[0][2]):
                _deny()
            self._tool = self._calls[0]
            return self._tool[0]

    def observe_tool(self, call_id, result):
        with self._lock:
            if self._tool is None or call_id != self._tool[0] or self._pending is not None:
                _deny()
            self._pending = bounded_copy([{'role': 'tool', 'tool_call_id': call_id,
                'tool_name': self._tool[1], 'content': result}])
            self._calls.pop(0)
            self._tool = None

    def result(self):
        with self._lock:
            if not self._finished or self._pending is not None:
                _deny()
            return {'final_response': self._messages[-1]['content'], 'api_calls': self._inferences,
                'completed': True, 'failed': False}
