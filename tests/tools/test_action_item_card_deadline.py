"""A card with a due date parks its Action Item with that date as the item's ``deadline``.

Before ``tasks.due_at`` the only deadline an item could carry was one the agent copied by hand, so a
customer date written on the card never reached Action Items.
"""
from __future__ import annotations

import json

import pytest

from tests.tools import test_action_item_tools as base
from tests.tools.test_action_item_tools import MERGE, _conn, as_worker, call, card_state, make_card

board = base.board  # the shared temp-board fixture, applied to every test below
pytestmark = pytest.mark.usefixtures("board")

DUE_EPOCH = 1796079600  # 2026-11-30T17:00:00-06:00


def _set_due(tid: str, due_at) -> None:
    from hermes_cli import kanban_db_due as kdue
    conn = _conn()
    try:
        assert kdue.set_due_at(conn, tid, due_at)
    finally:
        conn.close()


def _envelope(tid: str) -> dict:
    _, comments, _ = card_state(tid)
    body = comments[-1].body
    assert body.startswith("[action-item:v1] ")
    return json.loads(body[len("[action-item:v1] "):])


def test_dated_card_carries_due_at_into_the_item_deadline(monkeypatch):
    tid, run_id = make_card("engineering", claim=True)
    _set_due(tid, DUE_EPOCH)
    as_worker(monkeypatch, "engineering", tid, run_id)
    out = call("action_item_add", **MERGE)
    assert out["ok"], out
    assert _envelope(tid)["deadline"] == "2026-11-30T23:00:00+00:00"


def test_explicit_deadline_wins_over_the_card_due_date(monkeypatch):
    tid, run_id = make_card("engineering", claim=True)
    _set_due(tid, DUE_EPOCH)
    as_worker(monkeypatch, "engineering", tid, run_id)
    out = call("action_item_add", **MERGE, deadline="2026-11-20T09:00:00-06:00")
    assert out["ok"], out
    assert _envelope(tid)["deadline"] == "2026-11-20T09:00:00-06:00"


def test_undated_card_item_has_no_deadline(monkeypatch):
    tid, run_id = make_card("engineering", claim=True)
    as_worker(monkeypatch, "engineering", tid, run_id)
    out = call("action_item_add", **MERGE)
    assert out["ok"], out
    assert _envelope(tid)["deadline"] is None
