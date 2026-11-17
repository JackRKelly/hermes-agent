"""Kanban dashboard plugin: ``GET /board`` stays small and cheap.

A 564-task board served 2.0 MB per ``/board`` read (task bodies were 1.11 MB)
and spent most of its 0.3-0.5 s computing diagnostics for done cards nobody
inspects. Cards carry a body preview and only open work carries diagnostics;
the drawer reads the full body and diagnostics from ``GET /tasks/:id``.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_board_payload_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    app = FastAPI()
    app.include_router(_load_plugin_module().router, prefix="/api/plugins/kanban")
    return TestClient(app)


def _board_tasks(client) -> dict[str, dict]:
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200, r.text
    return {t["id"]: t for c in r.json()["columns"] for t in c["tasks"]}


def _cycled_task(conn, title: str) -> str:
    """A task carrying a real ``block_unblock_cycling`` diagnostic, a rule that does not
    look at status, so it fires on a done card too. The cycle history is written as
    events (the real block/unblock path routes repeat blocks to triage)."""
    task_id = kb.create_task(conn, title=title, assignee="alice")
    for i in range(4):
        kb._append_event(conn, task_id, "blocked", {"reason": f"wall {i}"})
        kb._append_event(conn, task_id, "unblocked")
    conn.commit()
    return task_id


def test_board_carries_a_body_preview_and_the_drawer_the_full_body(client):
    long_body = "".join(f"line {i:04d} of a long spec\n" for i in range(200))  # ~5 KB
    short_body = "short spec"
    long_id = client.post("/api/plugins/kanban/tasks", json={"title": "long", "body": long_body}).json()["task"]["id"]
    short_id = client.post("/api/plugins/kanban/tasks", json={"title": "short", "body": short_body}).json()["task"]["id"]

    cards = _board_tasks(client)
    assert cards[long_id]["body"] == long_body[:300]
    assert cards[long_id]["body_truncated"] is True
    assert cards[short_id]["body"] == short_body
    assert "body_truncated" not in cards[short_id]

    # The drawer (and its body editor) reads the detail endpoint: full text, no flag.
    detail = client.get(f"/api/plugins/kanban/tasks/{long_id}").json()["task"]
    assert detail["body"] == long_body
    assert "body_truncated" not in detail


def test_body_edit_round_trips_the_full_body_not_the_preview(client):
    task_id = client.post("/api/plugins/kanban/tasks", json={"title": "edit"}).json()["task"]["id"]
    edited = "x" * 1000 + " tail marker"
    r = client.patch(f"/api/plugins/kanban/tasks/{task_id}", json={"body": edited})
    assert r.status_code == 200, r.text

    assert _board_tasks(client)[task_id]["body"] == edited[:300]
    assert client.get(f"/api/plugins/kanban/tasks/{task_id}").json()["task"]["body"] == edited


def test_done_cards_carry_no_diagnostics_open_cards_keep_them(client):
    with kbc.connect() as conn:
        open_id = _cycled_task(conn, "still open")
        done_id = _cycled_task(conn, "finished")
        assert kb.complete_task(conn, done_id, summary="finished")
        assert kb.get_task(conn, done_id).status == "done"

    # The done task really has a diagnostic: the drawer still shows it.
    detail = client.get(f"/api/plugins/kanban/tasks/{done_id}").json()["task"]
    assert "block_unblock_cycling" in [d["kind"] for d in detail.get("diagnostics", [])]

    cards = _board_tasks(client)
    assert cards[done_id]["status"] == "done"
    assert "diagnostics" not in cards[done_id]
    assert "warnings" not in cards[done_id]
    assert "block_unblock_cycling" in [d["kind"] for d in cards[open_id]["diagnostics"]]
    assert cards[open_id]["warnings"]["count"] >= 1


def test_live_events_reload_the_board_throttled_and_skip_heartbeats():
    """Every WS event used to refetch the whole board after 250 ms, so a busy board
    re-downloaded it every few seconds per tab. Runs the real bundle functions on a fake
    clock: heartbeat-only batches never reload, the first reload after quiet is <= 250 ms,
    and a steady stream reloads at most once per 3 s without dropping the last change."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bundle = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    probe = Path(__file__).parent / "fixtures" / "kanban_reload_throttle_probe.js"
    result = subprocess.run([node, str(probe), str(bundle)], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "PASS" in result.stdout
