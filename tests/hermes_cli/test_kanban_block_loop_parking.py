"""A block-loop escalation stays parked until a human acts.

``_route_block`` sends a same-cause re-block to ``triage`` "for a human". The
triage sweeps (the dispatcher's auto-decompose tick, ``specify --all``) used
to list every triage task, so they promoted it ``triage -> todo -> ready``
within one tick; the respawned worker re-blocked for the same cause and the
task landed straight back in triage, hundreds of times. Approval-gate parks
(``needs_input`` with an ``[awaiting-approval:...]`` reason) re-block by
design and must stay in ``blocked``, where the approval sweep looks.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_specify as spec


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _claim(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def _block_unblock_reblock(conn, reason, kind: str | None = "needs_input"):
    tid = kb.create_task(conn, title="t", assignee="worker")
    _claim(conn, tid)
    assert kb.block_task(conn, tid, reason=reason, kind=kind)
    assert kb.unblock_task(conn, tid)
    _claim(conn, tid)
    assert kb.block_task(conn, tid, reason=reason, kind=kind)
    return tid


def test_triage_sweeps_skip_block_loop_escalations(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        looped = _block_unblock_reblock(conn, "CI keeps failing", kind="capability")
        assert kb.get_task(conn, looped).status == "triage"
        fresh = kb.create_task(conn, title="rough idea", assignee="worker", triage=True)

    assert decomp.list_triage_ids() == [fresh]
    assert spec.list_triage_ids() == [fresh]


def test_triage_sweeps_skip_untyped_block_loop_escalations(kanban_home: Path) -> None:
    # kanban_block without a kind, the goal-loop block and the dashboard
    # "blocked" status all block with kind=None; those loops reach triage too.
    with kbc.connect_closing() as conn:
        looped = _block_unblock_reblock(conn, "still stuck", kind=None)
        assert kb.get_task(conn, looped).status == "triage"

    assert decomp.list_triage_ids() == []
    assert spec.list_triage_ids() == []


def _dashboard_client():
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    plugin_file = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    mod_spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_block_loop_test", plugin_file)
    assert mod_spec is not None and mod_spec.loader is not None
    mod = importlib.util.module_from_spec(mod_spec)
    sys.modules[mod_spec.name] = mod
    mod_spec.loader.exec_module(mod)
    app = fastapi.FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def test_human_move_back_to_triage_is_sweepable_again(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_unblock_reblock(conn, "CI keeps failing", kind="capability")
        # A human specifies it by id (triage -> todo -> ready) ...
        assert kb.specify_triage_task(conn, tid)
    # ... and later drags it back to triage on the dashboard for a new reason.
    client = _dashboard_client()
    r = client.patch(f"/api/plugins/kanban/tasks/{tid}", json={"status": "triage"})
    assert r.status_code == 200, r.text
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"

    assert decomp.list_triage_ids() == [tid]
    assert spec.list_triage_ids() == [tid]


@pytest.mark.parametrize("reason", [
    "[awaiting-approval:merge] PR head=abc then=complete",
    "  \n[awaiting-approval:merge] PR head=abc then=complete",
])
def test_approval_gate_reblock_stays_blocked(kanban_home: Path, reason: str) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_unblock_reblock(conn, reason)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_recurrences == 2
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "block_loop_detected" not in kinds
