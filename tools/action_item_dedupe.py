"""One open action item per artifact, across every board and profile (Jack, Nov 12).

``dedupe_key`` is the artifact an item is about, the same key as ``dedupe_key`` in assistant
``scripts/action_items_readiness.py`` (the app, the digest and the autopilot sweep collapse on it):
``pr:<repo>#<n>``, ``email:<account>:<thread>``, ``message:<channel>:<conversation>``,
``post:<gate artifact>:<post|draft|preview|slot>``, else ``card:<board>:<card>:<title>``. Card-keyed
items only ever collide with their own card, so the add-time check can only refuse a second item
about the same PR, email thread, conversation or post.

``open_item_for`` scans every board's blocked cards (the boards live under the shared kanban root,
so every profile's items are on them) and returns the open item another card already holds for a
key. Read-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from tools.action_item_kinds import legacy
from tools.action_item_kinds.core import reason_item_hash
from tools.action_item_kinds.registry import legacy_payload
from tools.action_item_view import ItemRefused, load_item

# assistant ``action_items_readiness.ACCOUNTS``: a legacy park may name the mailbox by alias.
ACCOUNTS = {"work": "jack@cybership.io", "personal": "kcjackkelly@gmail.com"}
_NORM_RE = re.compile(r"[^a-z0-9]+")
_POST_TARGETS = ("post", "draft", "preview", "slot")


def dedupe_key(kind: Optional[str], payload: Any, reason: str, board: str, card: str, title: Any) -> str:
    p = payload if isinstance(payload, dict) else {}
    if kind == "pr_merge" and p.get("repo") and p.get("number"):
        return f"pr:{str(p['repo']).lower()}#{int(p['number'])}"
    if kind == "send_email" and p.get("thread_id"):
        account = ACCOUNTS.get(str(p.get("account") or ""), str(p.get("account") or "")).lower()
        return f"email:{account}:{p['thread_id']}"
    if kind == "send_message" and p.get("conversation_id"):
        return f"message:{p.get('channel')}:{p['conversation_id']}"
    gate = legacy.parse_gate(reason)
    if gate and gate[0] == "external-post":
        target = next((gate[2][k] for k in _POST_TARGETS if gate[2].get(k)), None)
        if target:
            return f"post:{gate[1]}:{target}"
    norm = _NORM_RE.sub(" ", str(title or "").lower()).strip()
    return f"card:{board}:{card}:{norm}"


@dataclass(frozen=True)
class OpenItem:
    id: str
    board: str
    card: str
    actor: Optional[str]
    key: str


class DuplicateItem(ItemRefused):
    """Another card already holds the open item for this artifact."""

    def __init__(self, existing: OpenItem):
        self.existing = existing
        super().__init__(
            f"{existing.id} is already the open item for this artifact ({existing.key}; card {existing.card} on "
            f"board {existing.board}, parked by {existing.actor or 'unknown'}). One open item per artifact: "
            "update or withdraw that item (or ask its owner to) instead of adding a second one")


def _db_path(conn) -> Optional[Path]:
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row[2]).resolve() if row and row[2] else None


def _board_dbs() -> Iterator[tuple[str, Path]]:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_boards import list_boards

    for meta in list_boards(include_archived=False):
        slug = meta.get("slug") or kb.DEFAULT_BOARD
        path = kb.kanban_home() / "kanban.db" if slug == kb.DEFAULT_BOARD else kb.board_dir(slug) / "kanban.db"
        if path.is_file():
            yield slug, path


def _still_open(kb, conn, view, task) -> bool:
    """Open until withdrawn, or decided by a verified answer: a forged decision comment does not
    free the artifact, and an unverifiable one (Telegram) keeps it until the card is unblocked."""
    from tools.action_item_decision import decision_for

    status = decision_for(view, kb.list_comments(conn, view.card), None, assignee=task.assignee)
    return status["status"] == "open" or (status["status"] == "decided" and not status["verified"])


def _project(kb, conn, slug: str, task) -> Optional[OpenItem]:
    """The open item a blocked card holds: a trusted action item, or a legacy free-text park read the
    way the app reads it. An untrusted item is never shown to Jack, so it holds no artifact."""
    from hermes_cli.kanban_db_repark import latest_park_event

    park = latest_park_event(conn, task.id)
    if park is None or park["kind"] != "blocked":
        return None  # the loop breaker's park is not an action item
    view = load_item(kb, conn, slug, task.id, int(park["id"]))
    if view.envelope is not None and view.trusted:
        kind, payload, title = view.classification["render_as"], view.envelope["payload"], view.envelope.get("title")
    elif reason_item_hash(view.reason) is None:
        kind, payload = legacy_payload(view.reason) or (None, {})
        title = task.title
    else:
        return None
    if not _still_open(kb, conn, view, task):
        return None
    return OpenItem(id=view.id, board=slug, card=task.id, actor=view.actor,
                    key=dedupe_key(kind, payload, view.reason, slug, task.id, title))


def open_item_for(key: str, *, this_conn, this_card: str) -> Optional[OpenItem]:
    """The open item another card holds for ``key``, on any board; None when the artifact is free.
    The caller's own card is never a duplicate of itself: its re-park supersedes (newest wins)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    if key.startswith("card:"):
        return None  # a card-keyed item names its own card, so no other card can hold it
    this_db = _db_path(this_conn)
    for slug, path in _board_dbs():
        same_board = path.resolve() == this_db
        conn = kbc.connect(db_path=path)
        try:
            for task in kb.list_tasks(conn, status="blocked"):
                if same_board and task.id == this_card:
                    continue
                found = _project(kb, conn, slug, task)
                if found is not None and found.key == key:
                    return found
        finally:
            conn.close()
    return None
