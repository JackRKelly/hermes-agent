"""One open action item per artifact across every board and profile (Jack, Nov 12).

``action_item_add`` refuses a second open item for the same PR / email thread / conversation / post
when another card holds it, names the existing item and writes nothing; the same card re-parking
supersedes (newest wins); a withdrawn or closed item frees the artifact. The keys match
``dedupe_key`` in JackRKelly/assistant ``scripts/action_items_readiness.py``.
"""
from __future__ import annotations

import pytest

from tests.tools.test_action_item_tools import (
    HEAD, MERGE, FakeGh, _conn, as_chat, as_worker, call, card_state, make_card,
)
from tools.action_item_dedupe import dedupe_key, open_item_for
from tools.registry import invalidate_check_fn_cache

THREAD = "18c1f0a2b3d4e5f6"


@pytest.fixture
def board(monkeypatch, tmp_path):
    """A temp kanban root (the default board) shared by every profile, as in test_action_item_tools."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("dashboard:\n  basic_auth:\n    username: jack\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_DB", "HERMES_PROFILE_NAME", "HERMES_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("tools.action_item_kinds.github.GhSources", FakeGh)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    invalidate_check_fn_cache()
    return home


# --- the keys --------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind, payload, reason, expected", [
    ("pr_merge", {"repo": "Cybership-IO/v2", "number": "4280", "head_sha": HEAD}, "", "pr:cybership-io/v2#4280"),
    ("send_email", {"account": "jack@cybership.io", "thread_id": THREAD}, "", f"email:jack@cybership.io:{THREAD}"),
    ("send_email", {"account": "work", "thread_id": THREAD}, "", f"email:jack@cybership.io:{THREAD}"),
    ("send_email", {"account": "personal", "thread_id": THREAD}, "", f"email:kcjackkelly@gmail.com:{THREAD}"),
    ("send_message", {"channel": "slack", "conversation_id": "D07ABC123"}, "", "message:slack:D07ABC123"),
    ("other", {}, "[awaiting-approval:external-post] linkedin draft=d-9 slot=mon", "post:linkedin:d-9"),
    ("other", {}, "[awaiting-approval:external-post] linkedin preview=p-2 slot=mon", "post:linkedin:p-2"),
    ("design_decision", {}, "[needs-jack:decision] x item=abc", "card:default:t_1:railway roll out staging prod"),
])
def test_each_artifact_has_the_assistant_key(kind, payload, reason, expected):
    assert dedupe_key(kind, payload, reason, "default", "t_1", "Railway: roll out -- staging & prod?") == expected


def test_a_kind_without_its_artifact_falls_back_to_its_own_card():
    assert dedupe_key("pr_merge", {"repo": "o/r"}, "", "ops", "t_2", "  Merge it!! ") == "card:ops:t_2:merge it"
    assert dedupe_key("other", {}, "[awaiting-approval:external-post] linkedin", "ops", "t_2", None) == "card:ops:t_2:"


# --- add-time refusal ------------------------------------------------------------------------------

def _worker_card(monkeypatch, assignee: str = "engineering") -> str:
    tid, run_id = make_card(assignee, claim=True)
    as_worker(monkeypatch, assignee, tid, run_id)
    return tid


def test_a_second_card_cannot_add_an_item_for_an_open_pr(board, monkeypatch):
    first = _worker_card(monkeypatch)
    held = call("action_item_add", **MERGE)
    assert held["ok"], held
    second = _worker_card(monkeypatch, "reviewer")
    before = card_state(second)
    out = call("action_item_add", **MERGE)
    assert out["existing_id"] == held["id"], out
    assert held["id"] in out["error"] and "pr:cybership-io/v2#4280" in out["error"]
    assert out["error"].endswith("Nothing was written.")
    assert card_state(second) == before and card_state(second)[0] == "running"
    other_pr = call("action_item_add", **{**MERGE, "payload": {**MERGE["payload"], "number": 4281}})
    assert other_pr["ok"], other_pr  # a different PR is a different artifact
    assert card_state(first)[0] == "blocked"


def test_an_open_item_on_another_board_refuses_the_add(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_db_boards import create_board

    create_board("ops")
    conn = kbc.connect(board="ops")
    try:
        theirs = kb.create_task(conn, title="card", assignee="captain-ops", created_by="captain-ops")
    finally:
        conn.close()
    as_chat(monkeypatch, "captain-ops")
    held = call("action_item_add", **MERGE, card=theirs, board="ops")
    assert held["ok"] and held["id"].startswith(f"ops:{theirs}@"), held
    mine = _worker_card(monkeypatch)
    out = call("action_item_add", **MERGE)
    assert out["existing_id"] == held["id"] and "board ops" in out["error"], out
    assert card_state(mine)[1] == []


def test_the_same_card_reparking_supersedes(board, monkeypatch):
    """Newest wins on the card's own re-park; another card is then refused against the NEW id."""
    tid = _worker_card(monkeypatch)
    old = call("action_item_add", **MERGE)
    conn = _conn()
    try:
        assert open_item_for("pr:cybership-io/v2#4280", this_conn=conn, this_card=tid) is None
        assert open_item_for("pr:cybership-io/v2#4280", this_conn=conn, this_card="t_other").id == old["id"]
    finally:
        conn.close()
    as_chat(monkeypatch, "engineering")
    new = call("action_item_update", id=old["id"], why="The head moved.")
    assert new["ok"] and new["replaces"] == old["id"], new
    _worker_card(monkeypatch, "reviewer")
    assert call("action_item_add", **MERGE)["existing_id"] == new["id"]


def test_a_withdrawn_or_closed_item_frees_the_artifact(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    tid = _worker_card(monkeypatch)
    withdrawn = call("action_item_add", **MERGE)
    as_chat(monkeypatch, "engineering")
    assert call("action_item_withdraw", id=withdrawn["id"], reason="PR closed")["ok"]
    second = _worker_card(monkeypatch, "reviewer")
    held = call("action_item_add", **MERGE)
    assert held["ok"], held
    conn = _conn()
    try:
        kb.unblock_task(conn, second)
        assert kb.complete_task(conn, second, result="merged")
    finally:
        conn.close()
    _worker_card(monkeypatch, "ops")
    assert call("action_item_add", **MERGE)["ok"]
    assert card_state(tid)[0] == "ready"


@pytest.mark.parametrize("reason, key", [
    (f"[awaiting-approval:send-email] https://mail.google.com/mail/u/work/#all/{THREAD} draft=r-1 thread={THREAD} "
     "account=work", f"email:jack@cybership.io:{THREAD}"),
    ("[awaiting-approval:send-message] slack channel=slack conversation=D07ABC123", "message:slack:D07ABC123"),
    ("[awaiting-approval:external-post] linkedin draft=d-9 slot=mon", "post:linkedin:d-9"),
])
def test_a_legacy_gate_park_holds_its_artifact(board, monkeypatch, reason, key):
    """A free-text gate park on another card is an open item about that thread / conversation / post."""
    from hermes_cli import kanban_db as kb
    tid, _ = make_card("engineering")
    conn = _conn()
    try:
        assert kb.block_task(conn, tid, reason=reason, kind="needs_input")
        found = open_item_for(key, this_conn=conn, this_card="t_new")
        assert found is not None and found.card == tid and found.key == key
        assert open_item_for(key, this_conn=conn, this_card=tid) is None
        kb.unblock_task(conn, tid)
        assert open_item_for(key, this_conn=conn, this_card="t_new") is None
    finally:
        conn.close()


# --- legacy merge_gate parks and stale heads -------------------------------------------------------

NEW_HEAD = "b" * 40


def _merge_gate_park(head: str) -> str:
    """A free-text merge park the way merge_gate.py writes it (today's v2#4322 duplicate)."""
    from hermes_cli import kanban_db as kb
    tid, _ = make_card("merge-gate")
    conn = _conn()
    try:
        assert kb.block_task(conn, tid, kind="needs_input", reason=(
            f"[awaiting-approval:merge] https://github.com/cybership-io/v2/pull/4280 head={head} then=complete"))
    finally:
        conn.close()
    return tid


def test_a_merge_gate_park_holds_its_pr_and_refuses_a_captain_add(board, monkeypatch):
    gate = _merge_gate_park(HEAD)
    conn = _conn()
    try:
        held = open_item_for("pr:cybership-io/v2#4280", this_conn=conn, this_card="t_new")
    finally:
        conn.close()
    assert held is not None and held.card == gate and held.legacy and held.head == HEAD
    mine = _worker_card(monkeypatch, "captain")
    before = card_state(mine)
    out = call("action_item_add", **MERGE)
    assert out["existing_id"] == held.id and out["existing_card"] == gate, out
    assert gate in out["error"] and held.id in out["error"] and f"at head {HEAD}" in out["error"]
    # update/withdraw refuse a legacy park, so the advice must be the one action that works
    assert "orchestrator unblocks" in out["how_to_clear"] and f"action_item_withdraw {held.id}" not in out["how_to_clear"]
    assert card_state(mine) == before


def test_a_merge_gate_park_with_a_short_head_holds_nothing():
    """A short ``head=`` is not a typed merge park: it falls back to its own card's key."""
    from tools.action_item_kinds.registry import legacy_payload
    reason = "[awaiting-approval:merge] https://github.com/cybership-io/v2/pull/4280 head=abc1234 then=complete"
    kind, payload = legacy_payload(reason) or (None, {})
    assert dedupe_key(kind, payload, reason, "default", "t_9", "card").startswith("card:default:t_9:")


def test_a_trusted_holder_at_the_same_head_is_cleared_by_its_parker(board, monkeypatch):
    held_card = _worker_card(monkeypatch)
    held = call("action_item_add", **MERGE)
    _worker_card(monkeypatch, "reviewer")
    out = call("action_item_add", **MERGE)
    assert out["existing_id"] == held["id"] and out["existing_card"] == held_card, out
    assert f"action_item_withdraw {held['id']}" in out["how_to_clear"] and "(engineering)" in out["how_to_clear"]
    assert f"at head {HEAD}" in out["error"]


@pytest.mark.parametrize("legacy_holder", [False, True])
def test_a_holder_at_a_stale_head_is_reported_not_blocking(board, monkeypatch, legacy_holder):
    """The PR moved on: the fresh head's add goes through and names the stale holder to clear."""
    if legacy_holder:
        stale_card = _merge_gate_park(HEAD)
    else:
        stale_card = _worker_card(monkeypatch)
        assert call("action_item_add", **MERGE)["ok"]
    monkeypatch.setattr(FakeGh, "head", NEW_HEAD)
    _worker_card(monkeypatch, "captain")
    out = call("action_item_add", **{**MERGE, "payload": {**MERGE["payload"], "head_sha": NEW_HEAD}})
    assert out["ok"], out
    [stale] = out["stale_items"]
    assert stale["card"] == stale_card and stale["head"] == HEAD and stale["id"].startswith(f"default:{stale_card}@")
    advice = "orchestrator unblocks" if legacy_holder else f"action_item_withdraw {stale['id']}"
    assert advice in stale["how_to_clear"]
    assert card_state(stale_card)[0] == "blocked"  # reported, never touched
