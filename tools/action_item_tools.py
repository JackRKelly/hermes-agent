"""``action_item_*`` tools: typed, schema-checked asks for Jack on a Kanban card (design §4.1).

Enabled for every profile (Root, captains, card workers, crew): the four tools are in the core tool
list and their check only hides them from ``delegate_task`` children, which never own a card. They
write to the shared board DB like the ``kanban_*`` tools, so they work on any terminal backend.
Agents create, update, withdraw and read items; no tool or argument records a decision.
"""

from __future__ import annotations

import functools
import json
import logging
from typing import Callable

from tools.action_item_kinds import readability as rd
from tools.action_item_kinds.core import SchemaError
from tools.action_item_kinds.registry import KINDS
from tools.action_item_view import ItemRefused
from tools.registry import no_cache_check_fn, registry, tool_error

logger = logging.getLogger(__name__)

_ID = {"type": "string", "description": "Item id <board>:<card>@<block_event_id>, as action_item_add returned it."}
_PLAIN = ("plain words: no raw tags, key=value fields, card ids, local paths, em dashes or board jargon "
          f"(the {rd.GUIDE} banned list)")
_ENVELOPE = {
    "title": {"type": "string", "description": (
        f"Required. What the card is about, 1-{rd.TITLE_MAX_WORDS} words, <={rd.TITLE_MAX_CHARS} chars, "
        f'{_PLAIN}. E.g. "{rd.EXAMPLE_TITLE}"')},
    "summary": {"type": "string", "description": (
        f"Required. 1-{rd.SUMMARY_MAX_SENTENCES} short sentences, <={rd.SUMMARY_MAX_CHARS} chars: what "
        f"happened and why Jack is needed now, {_PLAIN}.")},
    "ask": {"type": "string", "description": "One line (<=140 chars): the decision or action, as a question for Jack."},
    "why": {"type": "string", "description": "<=280 chars: why it matters and what waits on it."},
    "risk": {"type": "object", "description": f"{{level: low|medium|high, note: one line, <={rd.RISK_NOTE_MAX_CHARS} chars}}.",
             "properties": {"level": {"type": "string", "enum": ["low", "medium", "high"]},
                            "note": {"type": "string"}}, "required": ["level", "note"]},
    "deadline": {"type": "string", "description": "A real deadline only, ISO 8601 with a UTC offset."},
    "links": {"type": "array", "description": "<=5 {label, url}; https on github.com, linear.app, "
              "mail.google.com, app.slack.com or the dashboard host only.",
              "items": {"type": "object", "properties": {"label": {"type": "string"},
                                                         "url": {"type": "string"}}}},
    "then": {"type": "string", "enum": ["complete", "ready"],
             "description": "After Jack approves: complete the card, or hand it back ready. Defaults per kind."},
    "batchable": {"type": "boolean", "description": "pr_merge only: may be ticked into a batch merge."},
}
_KIND_PAYLOADS = (
    "Payload = the kind's AGENT fields only; the tool reads the rest from the live source. "
    "pr_merge: {repo, number, head_sha, method?} (the PR must be open at head_sha). "
    "send_email (frozen v1): {account, thread_id, draft_id, from, last_inbound_at, last_inbound_excerpt, "
    "to, cc, bcc, subject, body, attachments}. "
    "send_message (frozen v1): {channel, account, conversation_id, contact, recipients, last_messages, "
    "last_inbound_at, draft_text, draft_ref}. "
    f"design_decision: {{questions: [1-{rd.MAX_QUESTIONS} of {{key: \"q1\"..\"q{rd.MAX_QUESTIONS}\", "
    f"question: one question, <={rd.QUESTION_MAX_CHARS} chars, options: [{rd.MIN_OPTIONS}-{rd.MAX_OPTIONS} of "
    f"{{key, label <={rd.OPTION_LABEL_MAX_CHARS} chars, effect? <={rd.OPTION_EFFECT_MAX_CHARS} chars, "
    "recommended}] with exactly one recommended and key \"other\" reserved, allow_other? (default true: Jack "
    f"may answer in his own words)}}], rec_reason? <={rd.REC_REASON_MAX_CHARS} chars}}. Several questions go in "
    "ONE item as questions[], never numbered in one question; Jack answers them, there is no approve. "
    "user_task: {steps: [...], where}. other: {title, body, links?}. A refusal names the field, the rule "
    "and a conforming example: fix it and call again once."
)
# Jack, Nov 12 (JackRKelly/assistant docs/action-items-card-guide.md section 7): how the texts he reads
# are written, and the one place a local path may appear.
_WRITING = (
    " Writing for Jack (ask, why, risk.note, body, steps, effects, rec_reason): name PRs and Linear "
    "issues inline as #n, owner/repo#n or CYB-n (the app links them in place; never paste their URLs); "
    "wrap commands, env var names (`REDIS_PUBLIC_URL`), Redis/SQL keywords (`KEYS`, `FLUSH`, `EVAL`, "
    "`CONFIG`, `SELECT`), file names and identifiers in backticks; put a multi-step command in a fenced "
    "block with its language (```bash, sql, redis, json, ts or python); write steps as `1.` / `2.` lines. "
    "Never print a local path: a file the card shows (a post's image, a screenshot) is named by its "
    "path inside this card's own workspace (<kanban root>/kanban/workspaces/<card id>/) or LinkedIn "
    "preview dir (<kanban root>/state/linkedin/previews/<card id>/) and the app embeds it; any other "
    "local path is refused."
)

ACTION_ITEM_ADD_SCHEMA = {
    "name": "action_item_add",
    "description": (
        "Ask Jack for a decision or an action as a typed Action Item, instead of a free-text park. "
        "Validates the payload against the kind's schema (an invalid call writes nothing), fills the "
        "tool fields from the live source, writes the item comment and parks the card for Jack. "
        "Agents never approve: only Jack's decision resolves an item. The requester and the card come "
        "from this session (its own Kanban task, or a card you own passed as `card`), never from "
        "arguments. Parking your own task ends your run: stop after a successful add. One item per "
        "decision, and one open item per PR, email thread or conversation across every board: when another "
        "card already holds one, the add is refused with its existing_id and how_to_clear; an older park "
        "of the same PR at another head does not block, it comes back in stale_items to be cleared. "
        "Re-adding the exact draft Jack already approved on this card returns carried_from and hands a "
        "send back to you instead of asking him again. On a card parked with a free-text gate for the "
        "same PR head or draft, the add replaces that park (migrated_from). "
        + _KIND_PAYLOADS + _WRITING),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": sorted(KINDS)},
            "payload": {"type": "object", "description": "The kind's agent fields (see the description)."},
            **_ENVELOPE,
            "card": {"type": "string", "description": "A card you own, when this session has no Kanban task."},
            "board": {"type": "string", "description": "Board slug; defaults to the current board."},
        },
        "required": ["kind", "payload", "title", "summary", "ask", "why", "risk"],
    },
}
ACTION_ITEM_UPDATE_SCHEMA = {
    "name": "action_item_update",
    "description": (
        "Re-park an item you parked (or on a card you are assigned) with changed fields, e.g. the PR "
        "head moved or the draft was edited: a new item comment and a new park, so the item gets a new "
        "id and binding hash and any approval of the old id is void. Pass only what changed; payload "
        "fields merge over the current agent fields and tool fields are re-read live. The new item must "
        "meet today's rules: an item stored without title and summary needs both passed, and a v1 "
        "design_decision needs its questions passed as questions[] (see action_item_add)."),
    "parameters": {
        "type": "object",
        "properties": {"id": _ID, "payload": {"type": "object", "description": "Changed agent fields."},
                       **_ENVELOPE},
        "required": ["id"],
    },
}
ACTION_ITEM_WITHDRAW_SCHEMA = {
    "name": "action_item_withdraw",
    "description": (
        "Withdraw an open item that no longer needs Jack (PR closed, someone else did it): records the "
        "reason on the card and unblocks it back to its owner. Allowed for the profile that parked the "
        "item or the card's assignee."),
    "parameters": {
        "type": "object",
        "properties": {"id": _ID, "reason": {"type": "string", "description": "One line, <=500 chars."}},
        "required": ["id", "reason"],
    },
}
ACTION_ITEM_GET_DECISION_SCHEMA = {
    "name": "action_item_get_decision",
    "description": (
        "Read-only: Jack's answer to an item, {status, verdict, option, answers, note, decided_by, via, "
        "at, audit_id, verified}. A design_decision has verdict \"answer\": answers maps each question "
        "key to the option key Jack picked, or \"other\" with his own words in note. Act on a decision "
        "only when verified is true; an unverifiable decision comment is no approval. A re-park of the "
        "exact same PR head or draft on the same card keeps a verified, not yet acted on approval "
        "(carried_from names the item Jack approved; that item then reads status \"carried\"). outcome "
        "is done, failed or carried once the approval is used."),
    "parameters": {"type": "object", "properties": {"id": _ID}, "required": ["id"]},
}


@no_cache_check_fn
def _check_action_items() -> bool:
    """Every profile; never a delegate_task child (it owns no card and must not park its parent's)."""
    from tools.kanban_tools import _is_delegated_child_context

    return not _is_delegated_child_context()


def _handler(tool_name: str, op: Callable[[dict], dict]) -> Callable:
    @functools.wraps(op)
    def run(args: dict, **kw) -> str:
        properties = registry.get_schema(tool_name)["parameters"]["properties"]
        unknown = sorted(set(args) - set(properties))
        if unknown:
            return tool_error(f"{tool_name}: unknown parameter(s): {', '.join(unknown)}. Valid parameters: "
                              f"{', '.join(sorted(properties))}. Nothing was written.")
        try:
            return json.dumps(op(args))
        except (SchemaError, ItemRefused) as exc:
            existing = getattr(exc, "existing", None)
            extra = {"existing_id": existing.id, "existing_card": existing.card, "existing_board": existing.board,
                     "how_to_clear": existing.how_to_clear()} if existing is not None else {}
            return tool_error(f"{tool_name} refused: {exc}. Nothing was written.", **extra)
        except Exception as exc:  # health: allow BLE001 -- tool boundary: logged with traceback, returned as a tool error
            logger.exception("%s failed", tool_name)
            return tool_error(f"{tool_name}: {exc}")
    return run


def _ops():
    from tools import action_item_ops

    return action_item_ops


_TOOLS = (
    ("action_item_add", ACTION_ITEM_ADD_SCHEMA, lambda a: _ops().add_item(a), "🗂"),
    ("action_item_update", ACTION_ITEM_UPDATE_SCHEMA, lambda a: _ops().update_item(a), "🗂"),
    ("action_item_withdraw", ACTION_ITEM_WITHDRAW_SCHEMA, lambda a: _ops().withdraw_item(a), "🗂"),
    ("action_item_get_decision", ACTION_ITEM_GET_DECISION_SCHEMA, lambda a: _ops().get_decision(a), "🗂"),
)

for _name, _schema, _op, _emoji in _TOOLS:
    registry.register(name=_name, toolset="action_item", schema=_schema, handler=_handler(_name, _op),
                      emoji=_emoji, check_fn=_check_action_items)
