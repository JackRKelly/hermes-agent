"""Read Jack's decision on an action item and verify it (design §4.3). Strictly read-only.

A decision is a card comment ``[jack-approved:<id>]`` / ``[jack-rejected:<id>]`` / ``[jack-done:<id>]``
after the park. ``verified`` is true only for a ``source=webapp:<audit_id>`` decision that passes
every §4.3 check against the audit log at ``kanban_root()/action-items/audit.jsonl``:

1. exactly one row has the ``audit_id`` and the hash chain verifies up to it;
2. the row's item id, board, card, block event, binding hash, verdict, option and principal (Jack's
   ``dashboard.basic_auth.username``) equal the comment's and the current park's;
3. the row is newer than the park and the comment is not older than the row;
4. no other decision comment on the card cites the same ``audit_id``;
5. the comment is authored ``action-items`` (the web app) and the item itself is trusted (§3.1).

Audit row (one ``canonical_json`` object per line, written by the dashboard plugin): ``audit_id``,
``at`` (epoch seconds), ``principal``, ``item_id``, ``board``, ``card``, ``block_event_id``,
``binding_hash``, ``verdict``, ``option``, ``prev`` (``sha256:`` of the previous line's bytes, null
on the first line), plus free fields (session hash, client ip, note, result). A Telegram
``source=`` stays the approvals handler's check (it reads ``state.db``), so it reports
``verified: false`` here with that reason.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional

from tools.action_item_kinds.core import WITHDRAWN_PREFIX
from tools.action_item_kinds.hostcfg import dashboard_principal
from tools.action_item_view import ItemView

WEBAPP_AUTHOR = "action-items"
_DECISION_RE = re.compile(r"^\[jack-(?P<verdict>approved|rejected|done):(?P<id>[^\]\s]+)\]\s*(?P<rest>.*)$", re.DOTALL)
_VERDICT = {"approved": "approve", "rejected": "reject", "done": "done"}
# Row verdicts each comment prefix may carry (§3.3 step 6).
_ROW_VERDICTS = {"approved": {"approve", "pick_option", "other"}, "rejected": {"reject", "edit"},
                 "done": {"done"}}
_PARAM_RE = re.compile(r"(?<!\S)(source|binding|option|kind|base|head)=(\S+)")
_AUDIT_ID_RE = re.compile(r"^webapp:([A-Za-z0-9_-]{1,64})$")


def audit_log_path() -> Path:
    from hermes_cli.kanban_db import kanban_home

    return kanban_home() / "action-items" / "audit.jsonl"


def parse_decision(body: str) -> Optional[dict]:
    """``{prefix, verdict, id, note, params}`` of a decision comment, else None. The note is the
    quoted text; parameters are read only after its closing quote, so a note cannot forge them."""
    match = _DECISION_RE.match((body or "").lstrip())
    if not match:
        return None
    rest = match["rest"]
    note, tail = "", rest
    if rest.startswith('"') and '"' in rest[1:]:
        quoted, _, tail = rest[1:].rpartition('"')
        note = quoted
    params = dict(_PARAM_RE.findall(tail))
    option = params.get("option")
    if option is None:
        picked = re.fullmatch(r"web: approve option=(\S+)", note)
        option = picked.group(1) if picked else None
    return {"prefix": match["verdict"], "verdict": _VERDICT[match["verdict"]], "id": match["id"],
            "note": note, "params": params, "option": option}


def _read_audit(path: Path, audit_id: str) -> tuple[Optional[dict], Optional[str]]:
    """``(row, None)`` for a verified row, else ``(None, reason)``."""
    try:
        lines = path.read_bytes().split(b"\n")
    except OSError:
        return None, "audit log unreadable"
    prev: Optional[str] = None
    found: list[dict] = []
    chain_ok_at_found = False
    for raw in (line for line in lines if line):
        try:
            row = json.loads(raw)
        except ValueError:
            return None, "audit log has a malformed row"
        chain_ok = isinstance(row, dict) and row.get("prev") == prev
        if isinstance(row, dict) and row.get("audit_id") == audit_id:
            found.append(row)
            chain_ok_at_found = chain_ok and not found[:-1]
        if not chain_ok and not found:
            return None, "audit hash chain broken before the row"
        prev = "sha256:" + hashlib.sha256(raw).hexdigest()
    if len(found) != 1:
        return None, f"{len(found)} audit rows carry {audit_id}, not exactly one"
    return (found[0], None) if chain_ok_at_found else (None, "audit hash chain broken at the row")


def _row_problems(row: dict, view: ItemView, decision: dict, comment: Any) -> list[str]:
    binding = view.classification.get("binding_hash")
    problems = []
    expected = {"item_id": view.id, "board": view.board, "card": view.card,
                "block_event_id": view.event_id, "binding_hash": binding, "option": decision["option"]}
    problems += [f"audit {key} mismatch" for key, value in expected.items() if row.get(key) != value]
    if decision["params"].get("binding", binding) != binding:
        problems.append("comment binding= is not the item's binding hash")
    if row.get("verdict") not in _ROW_VERDICTS[decision["prefix"]]:
        problems.append("audit verdict does not match the comment")
    principal = dashboard_principal()
    if principal is None or row.get("principal") != principal:
        problems.append("audit principal is not dashboard.basic_auth.username")
    at = row.get("at")
    if not isinstance(at, (int, float)) or at < view.event_at or comment.created_at < int(at):
        problems.append("audit time is not between the park and the comment")
    return problems


def verify(view: ItemView, decision: dict, comment: Any, all_comments: list) -> tuple[bool, list[str]]:
    source = decision["params"].get("source", "")
    audit = _AUDIT_ID_RE.match(source)
    if not audit:
        why = "telegram source: verified by the approvals handler" if source.startswith("telegram:") \
            else "no source=webapp:<audit_id>"
        return False, [why]
    problems = [] if view.trusted else ["the item is not trusted: " + "; ".join(view.problems)]
    if comment.author != WEBAPP_AUTHOR:
        problems.append(f"decision comment not authored {WEBAPP_AUTHOR}")
    cited = [c for c in all_comments if c.id != comment.id and (parsed := parse_decision(c.body))
             and parsed["params"].get("source") == source]
    if cited:
        problems.append("another decision comment cites the same audit_id")
    row, why = _read_audit(audit_log_path(), audit.group(1))
    problems += [why] if row is None else _row_problems(row, view, decision, comment)
    return not problems, problems


def _pick_decision(view: ItemView, window: list, comments: list) -> Optional[tuple]:
    """``(comment, decision, verified, problems)`` for the item: the first verified decision, else
    the first one, so an earlier forged comment cannot mask Jack's later verified answer."""
    found = []
    for comment in window:
        decision = parse_decision(comment.body)
        if decision is None or decision["id"] != view.id:
            continue
        verified, problems = verify(view, decision, comment, comments)
        if verified:
            return comment, decision, verified, problems
        found.append((comment, decision, verified, problems))
    return found[0] if found else None


def decision_for(view: ItemView, comments: list, next_park_at: Optional[int],
                 assignee: Optional[str] = None) -> dict:
    """The ``action_item_get_decision`` result for one resolved item."""
    window = [c for c in comments if c.created_at >= view.event_at
              and (next_park_at is None or c.created_at < next_park_at)]
    out: dict = {"id": view.id, "status": "open", "verdict": None, "option": None, "note": None,
                 "decided_by": None, "via": None, "at": None, "audit_id": None, "verified": False,
                 "trusted_item": view.trusted, "problems": list(view.problems)}
    # Only the owners withdraw (§4.2), so a withdrawal is theirs and cites this item's id.
    withdrawn = f"{WITHDRAWN_PREFIX}:{view.id}]"
    owners = {owner for owner in (view.actor, assignee) if owner}
    if any(c.body.startswith(withdrawn) and c.author in owners for c in window):
        out["status"] = "withdrawn"
    picked = _pick_decision(view, window, comments)
    if picked is not None:
        comment, decision, verified, problems = picked
        source = decision["params"].get("source", "")
        out.update(status="decided", verdict=decision["verdict"], option=decision["option"],
                   note=decision["note"], decided_by=comment.author, at=comment.created_at,
                   via=source.split(":", 1)[0] or None, verified=verified, problems=problems,
                   audit_id=source.split(":", 1)[1] if source.startswith("webapp:") else None)
        after = [c.body for c in window if c.created_at >= comment.created_at and c.id > comment.id]
        if any(body.startswith("[action-done") for body in after):
            out["status"] = "done"
        elif any(body.startswith("[action-failed") for body in after):
            out["status"] = "failed"
    if next_park_at is not None and out["status"] == "open":
        out["status"] = "superseded"
    return out
