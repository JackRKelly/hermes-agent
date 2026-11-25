"""Stateless (2026-07-28) form elicitation for Google Workspace MCP servers.

A fake Workspace server speaks the wire Google documents at
https://developers.google.com/workspace/guides/mcp-elicitation behind ``httpx2.MockTransport``,
and the REAL mcp 2.x ``ClientSession`` + Streamable HTTP transport talk to it, negotiated through
Hermes' ``protocol: stateless`` path. Each test then drives a ``tools/call`` through Hermes' call
path and answers the approval through a patched consent surface (or the real one, unattended).

The wire tests need mcp >= 2.0 (the 2026-07-28 era); the pure helpers below them run on any SDK.
"""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import mcp_tool_input_required as input_required
from tools.mcp_tool_errors import _mcp_call_failed_message
from tools.mcp_tool_sampling import ApprovalChoiceForm, ElicitationHandler, approval_choice_form

URL = "https://calendarmcp.googleapis.com/mcp/v1"
ARGS = {"calendarId": "primary", "summary": "Architecture Review",
        "attendees": [{"email": "dana@example.com"}]}
MESSAGE = "**Architecture Review**  \nTue, Oct 20 · 10:00 - 10:30 AM PDT  \nYou, dana@example.com"
QUESTION = "Allow hermes-agent to use create_event for these recipients?"
ENUM = ["ALLOW_ONCE", "ALLOW_AUDIENCE_ALWAYS", "ALLOW_TOOL_ALWAYS", "DENY_ONCE"]
ENUM_NAMES = ["Allow once", "Always allow for these recipients", "Always allow this tool", "Don't allow"]
REQUEST_STATE = '{"elicitationId":"e-1","requestBinding":"b-1"}'
DENIED_TEXT = "Action canceled. The user declined the confirmation request."
CAPABILITY_ERROR = ("Action could not be completed. This action requires user confirmation, which this client "
                    "does not support. Use a client that supports form elicitation to perform this action.")
CAPS_KEY = "io.modelcontextprotocol/clientCapabilities"


def _approval_schema(enum=ENUM, names=ENUM_NAMES):
    return {"type": "object", "required": ["approval_choice"], "properties": {"approval_choice": {
        "type": "string", "title": QUESTION, "enum": list(enum), "enumNames": list(names)}}}


class FakeWorkspaceServer:
    """Google Workspace MCP's stateless wire: ``server/discover``, ``tools/list`` and a gated
    ``create_event`` that needs approval unless the retry carries a bound answer.

    Every request is recorded as ``(headers, body)`` so tests can assert what Hermes sent.
    ``rechallenge`` makes the server answer every retry with a fresh challenge (a broken server).
    """

    def __init__(self, *, rechallenge: bool = False):
        self.rechallenge = rechallenge
        self.requests: list = []

    def calls(self, method: str) -> list:
        return [body for _, body in self.requests if body.get("method") == method]

    @staticmethod
    def _reply(rid, result=None, error=None, status=200):
        import httpx2
        payload = {"jsonrpc": "2.0", "id": rid, **({"error": error} if error else {"result": result})}
        return httpx2.Response(status, json=payload)

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append((dict(request.headers), body))
        method, rid, params = body.get("method"), body.get("id"), body.get("params") or {}
        # The live server rejects a request whose headers do not match its body (-32020).
        if (request.headers.get("mcp-protocol-version") != "2026-07-28"
                or request.headers.get("mcp-method") != method):
            return self._reply(rid, error={"code": -32020, "message": "Header mismatch"}, status=400)
        if method == "server/discover":
            return self._reply(rid, {
                "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "calendarmcp", "version": "ESF"}},
                "cacheScope": "public", "capabilities": {"tools": {"listChanged": False}},
                "resultType": "complete", "supportedVersions": ["2025-11-25", "2026-07-28"], "ttlMs": 0})
        if method == "tools/list":
            return self._reply(rid, {"cacheScope": "public", "resultType": "complete", "ttlMs": 0, "tools": [{
                "name": "create_event", "description": "Creates a calendar event.",
                "inputSchema": {"type": "object", "properties": {"summary": {"type": "string"}}}}]})
        if method != "tools/call":
            return self._reply(rid, error={"code": -32601, "message": "Method not found"})
        caps = (params.get("_meta") or {}).get(CAPS_KEY) or {}
        if "elicitation" not in caps:
            return self._reply(rid, error={"code": -32021, "message": CAPABILITY_ERROR, "data": {
                "requiredCapabilities": {"elicitation": {"form": {}}}}})
        answer = (params.get("inputResponses") or {}).get("approval_choice")
        if answer is None or self.rechallenge:
            return self._reply(rid, {"resultType": "input_required", "requestState": REQUEST_STATE,
                                     "inputRequests": {"approval_choice": {
                                         "method": "elicitation/create",
                                         "params": {"mode": "form", "message": MESSAGE,
                                                    "requestedSchema": _approval_schema()}}}})
        # The server binds requestState to the first call's exact arguments.
        assert params.get("requestState") == REQUEST_STATE
        assert params.get("arguments") == ARGS
        choice = (answer.get("content") or {}).get("approval_choice") if answer.get("action") == "accept" else None
        if choice in ("ALLOW_ONCE", "ALLOW_AUDIENCE_ALWAYS", "ALLOW_TOOL_ALWAYS"):
            return self._reply(rid, {"resultType": "complete", "isError": False,
                                     "content": [{"type": "text", "text": f"created ({choice})"}]})
        return self._reply(rid, {"resultType": "complete", "isError": True,
                                 "content": [{"type": "text", "text": DENIED_TEXT}]})


def _mcp2():
    """Skip the wire tests on mcp 1.x: it has no 2026-07-28 era to test."""
    pytest.importorskip("httpx2")
    session_mod = pytest.importorskip("mcp.client.session")
    if not hasattr(session_mod.ClientSession, "discover"):
        pytest.skip("mcp < 2.0 has no stateless (2026-07-28) client")


@asynccontextmanager
async def _connected(fake, *, elicitation_config=None):
    """A real mcp 2.x session to *fake*, negotiated by Hermes with ``protocol: stateless``.

    ``elicitation_config=False`` connects with elicitation disabled (no handler registered).
    """
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    from tools.mcp_tool import MCPServerTask
    from tools.mcp_tool_common import _core

    task = MCPServerTask("google_calendar")
    task._config = {"url": URL, "protocol": "stateless"}
    task._sampling = None
    task._elicitation = (None if elicitation_config is False
                         else ElicitationHandler("google_calendar", elicitation_config or {"timeout": 5}))
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(fake)) as http:
        async with streamable_http_client(URL, http_client=http, terminate_on_close=False) as streams:
            async with _core.ClientSession(streams[0], streams[1], **task._session_kwargs()) as session:
                await task._negotiate_session(session, 5)
                await session.list_tools()
                yield SimpleNamespace(session=session), task


def _call(fake, *, elicitation_config=None):
    """One ``create_event`` call through Hermes' call path; returns ``(result, task)``."""
    from tools.mcp_tool_handlers import _call_tool_racing_stdio_death

    async def run():
        async with _connected(fake, elicitation_config=elicitation_config) as (server, task):
            # Caught inside the session: raised through its task group it would arrive wrapped in an
            # ExceptionGroup, unlike production, where the call runs outside the session's context.
            try:
                return await _call_tool_racing_stdio_death(server, "google_calendar", "create_event",
                                                           dict(ARGS)), task
            except Exception as exc:  # noqa: BLE001
                failure = exc
        raise failure
    return asyncio.run(run())


def _text(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


def _metrics(task) -> dict:
    assert task._elicitation is not None
    return task._elicitation.metrics


def _answers(fake) -> list:
    """The ``inputResponses`` Hermes sent on each retry."""
    return [call["params"].get("inputResponses") for call in fake.calls("tools/call")[1:]]


class TestStatelessWire:
    def test_capability_declared_in_meta_on_list_and_call(self):
        _mcp2()
        fake = FakeWorkspaceServer()
        with patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
            _call(fake)
        for method in ("tools/list", "tools/call"):
            bodies = fake.calls(method)
            assert bodies, method
            for body in bodies:
                meta = body["params"]["_meta"]
                assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
                assert meta[CAPS_KEY]["elicitation"]["form"] == {}
                assert meta["io.modelcontextprotocol/clientInfo"]["name"] == "hermes-agent"

    def test_allow_once_shows_google_text_verbatim_and_retries_with_bound_answer(self):
        _mcp2()
        fake = FakeWorkspaceServer()
        seen = {}

        def consent(message, description, **kwargs):
            seen.update(message=message, description=description, **kwargs)
            return "accept"
        with patch("tools.approval_prompt.request_elicitation_consent", side_effect=consent):
            result, task = _call(fake)
        assert not result.is_error and _text(result) == "created (ALLOW_ONCE)"
        assert seen["message"] == MESSAGE
        assert seen["title"] == QUESTION
        for value, label in zip(ENUM, ENUM_NAMES):
            assert f"{label} ({value})" in seen["description"]
        assert seen["surface"] == "mcp-elicitation/google_calendar"
        retry = fake.calls("tools/call")[1]["params"]
        assert retry["requestState"] == REQUEST_STATE
        assert retry["arguments"] == ARGS
        assert retry["inputResponses"] == {"approval_choice": {
            "action": "accept", "content": {"approval_choice": "ALLOW_ONCE"}}}
        assert _metrics(task)["accepted"] == 1

    def test_deny_sends_deny_once_and_returns_the_servers_denial(self):
        _mcp2()
        fake = FakeWorkspaceServer()
        with patch("tools.approval_prompt.request_elicitation_consent", return_value="decline"):
            result, task = _call(fake)
        assert result.is_error and _text(result) == DENIED_TEXT
        assert _answers(fake) == [{"approval_choice": {"action": "accept",
                                                       "content": {"approval_choice": "DENY_ONCE"}}}]
        assert _metrics(task)["declined"] == 1

    def test_timeout_cancels_and_never_accepts(self, monkeypatch):
        _mcp2()
        monkeypatch.setattr(ElicitationHandler, "_OUTER_TIMEOUT_GRACE_SECONDS", 0)
        fake = FakeWorkspaceServer()

        def slow_consent(*_args, **_kwargs):
            time.sleep(1.5)
            return "accept"  # arrives after the deadline: must never be sent
        with patch("tools.approval_prompt.request_elicitation_consent", side_effect=slow_consent):
            result, _task = _call(fake, elicitation_config={"timeout": 1})  # 1 s is the config minimum
        assert result.is_error and _text(result) == DENIED_TEXT
        assert _answers(fake) == [{"approval_choice": {"action": "cancel"}}]

    def test_unattended_session_declines_without_prompting(self, monkeypatch):
        _mcp2()
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        fake = FakeWorkspaceServer()
        with patch("tools.approval_prompt.prompt_dangerous_approval") as prompt:
            result, _task = _call(fake)
        prompt.assert_not_called()
        assert result.is_error and _text(result) == DENIED_TEXT
        assert _answers(fake) == [{"approval_choice": {"action": "accept",
                                                       "content": {"approval_choice": "DENY_ONCE"}}}]

    def test_rechallenging_server_stops_after_bounded_rounds(self):
        _mcp2()
        fake = FakeWorkspaceServer(rechallenge=True)
        with patch("tools.approval_prompt.request_elicitation_consent", return_value="accept") as consent:
            with pytest.raises(input_required.InputRequiredRoundsExceeded, match="Do NOT retry"):
                _call(fake)
        rounds = input_required.MAX_INPUT_REQUIRED_ROUNDS
        assert consent.call_count == rounds
        assert len(fake.calls("tools/call")) == rounds + 1

    def test_missing_capability_error_is_surfaced_with_the_fix(self):
        _mcp2()
        fake = FakeWorkspaceServer()
        with pytest.raises(Exception) as excinfo:
            _call(fake, elicitation_config=False)
        assert "elicitation" not in fake.calls("tools/call")[0]["params"]["_meta"].get(CAPS_KEY, {})
        message = _mcp_call_failed_message(excinfo.value)
        assert "requires user confirmation" in message
        assert "protocol: stateless" in message and "Do NOT retry" in message


class TestApprovalChoiceForm:
    def test_google_form_is_recognized_in_server_order(self):
        form = approval_choice_form(_approval_schema())
        assert form == ApprovalChoiceForm("approval_choice", QUESTION, tuple(zip(ENUM, ENUM_NAMES)))

    def test_one_of_labels_are_used(self):
        schema = {"properties": {"c": {"type": "string", "oneOf": [
            {"const": "ALLOW_ONCE", "title": "Allow once"}, {"const": "DENY_ONCE"}]}}}
        form = approval_choice_form(schema)
        assert form is not None
        assert form.options == (("ALLOW_ONCE", "Allow once"), ("DENY_ONCE", "DENY_ONCE"))

    @pytest.mark.parametrize("schema", [
        {},
        {"properties": {"approved": {"type": "boolean"}}},
        {"properties": {"c": {"type": "string", "enum": ["YES", "NO"]}}},
        {"properties": {"c": {"enum": ["ALLOW_ONCE"]}, "note": {"type": "string"}}},
    ])
    def test_other_forms_keep_the_generic_path(self, schema):
        assert approval_choice_form(schema) is None

    def test_form_without_deny_once_declines_on_no(self):
        handler = ElicitationHandler("s", {"timeout": 5})
        params = SimpleNamespace(mode="form", message="m",
                                 requested_schema={"properties": {"c": {"enum": ["ALLOW_ONCE"]}}})
        with patch("tools.approval_prompt.request_elicitation_consent", return_value="decline"):
            result = asyncio.run(handler(None, params))
        assert result.action == "decline" and not result.content

    def test_yes_never_sends_a_standing_grant(self):
        handler = ElicitationHandler("s", {"timeout": 5})
        params = SimpleNamespace(mode="form", message="m", requested_schema=_approval_schema(
            enum=["ALLOW_TOOL_ALWAYS", "ALLOW_ONCE", "DENY_ONCE"], names=["Always", "Once", "No"]))
        with patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
            result = asyncio.run(handler(None, params))
        assert result.content == {"approval_choice": "ALLOW_ONCE"}


class _RpcError(Exception):
    """SDK-agnostic stand-in for ``MCPError``: a message plus ``.error.code``."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.error = SimpleNamespace(code=code)


class TestCapabilityErrorMessage:
    def test_minus_32021_gets_the_fix(self):
        message = _mcp_call_failed_message(_RpcError(CAPABILITY_ERROR, -32021))
        assert CAPABILITY_ERROR in message and "protocol: stateless" in message

    def test_other_errors_are_unchanged(self):
        assert _mcp_call_failed_message(_RpcError("boom", -32000)) == "MCP call failed: _RpcError: boom"


class TestPlainSessionsKeepThePlainCall:
    def test_mcp1_and_test_doubles_are_not_driven(self):
        from unittest.mock import MagicMock

        async def call_tool(name, arguments=None, read_timeout_seconds=None, progress_callback=None):
            return None
        assert input_required.supports_input_required(MagicMock()) is False
        assert input_required.supports_input_required(SimpleNamespace(call_tool=call_tool)) is False
