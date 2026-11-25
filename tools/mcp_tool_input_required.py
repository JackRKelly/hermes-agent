"""Stateless (2026-07-28) ``tools/call`` approvals: the ``input_required`` retry loop.

A stateless MCP server cannot send ``elicitation/create`` to the client mid-call. Instead it
answers ``tools/call`` with ``resultType: "input_required"``: a map of embedded input requests
(Google Workspace sends one form keyed ``approval_choice``) plus an opaque ``requestState``.
The client asks the user, then re-sends the SAME tool name and arguments with
``inputResponses`` and the echoed ``requestState``; the server binds that state to the exact
arguments, so a retry can never approve a different call.

mcp >= 2.0 parses the result and stamps the per-request ``_meta`` capability block, but its
``ClientSession.call_tool`` raises on ``input_required`` unless the caller drives the loop.
This module is that loop for Hermes: each embedded request goes through the session's own
callback table (``ClientSession.dispatch_input_request``), so an approval form reaches the
same :class:`tools.mcp_tool_sampling.ElicitationHandler` (CLI, TUI, gateway approval UI) as a
legacy server-initiated ``elicitation/create``. Docs:
https://developers.google.com/workspace/guides/mcp-elicitation
"""

import asyncio
import inspect
import logging
from typing import Any, Optional

logger = logging.getLogger("tools.mcp_tool")

# Most rounds one tools/call may spend answering ``input_required`` before Hermes gives up.
# Google's reference clients use 3: one approval, plus room for a server that re-challenges
# once (an unbound or expired requestState). A server that keeps asking past this is broken
# or adversarial, and every round may put a prompt in front of the user.
MAX_INPUT_REQUIRED_ROUNDS = 3

# Pause before retrying a round that carried only ``requestState`` (the server is still
# working and asked to be polled; the spec says back off rather than spin).
_STATE_ONLY_RETRY_DELAY_SECONDS = 0.25


class InputRequiredRoundsExceeded(RuntimeError):
    """The server kept answering ``input_required`` past :data:`MAX_INPUT_REQUIRED_ROUNDS`.

    Deliberately not a transport error: the session-expired recoverer must never replay a
    write call that may already have been approved.
    """


class InputRequestRefused(RuntimeError):
    """A client callback refused an embedded input request (returned ``ErrorData``)."""


def supports_input_required(session: Any) -> bool:
    """True when ``session.call_tool`` can hand back an ``InputRequiredResult`` (mcp >= 2.0).

    mcp 1.x has no stateless era at all, and test doubles (``MagicMock``) take ``*args,
    **kwargs``; both keep the plain ``call_tool`` path byte for byte.
    """
    try:
        params = inspect.signature(session.call_tool).parameters
    except (TypeError, ValueError, AttributeError):
        return False
    return "allow_input_required" in params


def _is_input_required(result: Any) -> bool:
    """``InputRequiredResult`` by its wire tag, so the check needs no SDK type import."""
    return getattr(result, "result_type", None) == "input_required"


async def _answer_input_requests(session: Any, server_name: str, requests: dict) -> dict:
    """Run every embedded request through the session's callbacks, one at a time.

    Sequential on purpose: each one can be a prompt in front of the user, and two approval
    dialogs racing for one human answer is how the wrong one gets approved.
    """
    from mcp.client.context import ClientRequestContext  # mcp >= 2.0 only; gated by the caller
    from mcp.types import ErrorData

    responses: dict = {}
    for key, request in requests.items():
        params = getattr(request, "params", None)
        ctx = ClientRequestContext(session=session, request_id=key, meta=getattr(params, "meta", None))
        response = await session.dispatch_input_request(ctx, request)
        if isinstance(response, ErrorData):
            # ErrorData: the callback refused (e.g. elicitation disabled for this server). Abort the
            # call rather than invent an answer the user never gave.
            raise InputRequestRefused(
                f"MCP server '{server_name}' asked for input ({key}) that this client refused: {response.message}")
        responses[key] = response
    return responses


async def call_tool_answering_input_required(session: Any, server_name: str, tool_name: str, args: dict,
                                             progress_callback: Optional[Any] = None) -> Any:
    """``session.call_tool`` that resolves stateless ``input_required`` rounds before returning.

    Every retry re-sends the identical *args* with the server's ``requestState`` echoed
    byte-for-byte. The answer itself comes from the registered elicitation handler, which fails
    closed (decline or cancel) on timeout, error, or a session nobody can answer; this loop
    never fabricates an answer. Raises :class:`InputRequiredRoundsExceeded` after
    :data:`MAX_INPUT_REQUIRED_ROUNDS` rounds.
    """
    extra = {"progress_callback": progress_callback} if progress_callback is not None else {}
    result = await session.call_tool(tool_name, arguments=args, allow_input_required=True, **extra)
    rounds = 0
    while _is_input_required(result):
        rounds += 1
        if rounds > MAX_INPUT_REQUIRED_ROUNDS:
            raise InputRequiredRoundsExceeded(
                f"MCP server '{server_name}' kept asking for input on '{tool_name}' after "
                f"{MAX_INPUT_REQUIRED_ROUNDS} answered rounds; Hermes stopped the call. "
                "Do NOT retry it; tell the user the server's approval flow did not complete.")
        requests = getattr(result, "input_requests", None) or {}
        if requests:
            logger.info("MCP server '%s': %s needs input (%s), round %d",
                        server_name, tool_name, ", ".join(requests), rounds)
            responses = await _answer_input_requests(session, server_name, requests)
        else:
            await asyncio.sleep(_STATE_ONLY_RETRY_DELAY_SECONDS)
            responses = None
        result = await session.call_tool(tool_name, arguments=args, input_responses=responses,
                                         request_state=getattr(result, "request_state", None),
                                         allow_input_required=True, **extra)
    return result
