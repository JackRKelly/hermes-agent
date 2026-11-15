"""A card attempt the owner gateway stopped for its own restart is a reclaim, never a crash.

Regression for the Nov 11 restarts: every in-flight card worker died inside the restart wait, was
booked ``crashed``, and the same-error systemic rule (>= 3 identical crashes in one tick) blocked
six cards at once (``gave_up``, effective limit 1). The worker side no longer gives up inside the
wait (``test_kanban_worker_restart_wait``); a turn the gateway still has to cut (the wait's cap,
or a plain shutdown) is noted on the run by the owner, and the dead-worker sweep requeues it
without charging the breaker. A real non-zero exit still books ``crashed``.

Nov 14 00:08: a gateway killed partway through its own shutdown never wrote that note, so 8 of
the 11 swept cards were blocked as one systemic crash (a 9th, an ordinary crash, used up its own
budget). The successor's ``interrupted`` settlement of an admission whose owner is gone now speaks
for it, once per card in a row: an owner that dies again under the card's next attempt is charged.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_owner_recovery import OWNER_EXITED_CAUSE
from hermes_cli.process_identity import worker_birth

NOW = int(time.time())
HOST = kb._claimer_id().split(":", 1)[0]
FINGERPRINT = "6a232414-2dbf-40ab-a534-d9b71db05658:116|4162240"
RESTARTING = "Gateway restarting"


def _absent_pid(base: int) -> int:
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kbc.connect()
    ledger = tmp_path / "state.db"
    with sqlite3.connect(ledger) as store:
        store.execute("CREATE TABLE session_admissions "
                      "(target_session_id TEXT, request_id TEXT, status TEXT, outcome TEXT)")
    yield SimpleNamespace(conn=conn, ledger=ledger)
    conn.close()


def _event(conn, tid, run_id, kind, payload):
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                 (tid, run_id, kind, json.dumps(payload), NOW - 10))


LIVE_OWNER = {"pid": os.getpid(), "birth": worker_birth(psutil.Process())}


def _running(board, tid, n, *, exit_code, stop=None, stop_lock=None, owner_gone=False, lost=None):
    """A ``running`` card whose managed attempt ended: its submitter and bound interpreter are dead,
    the owner settled the admission ``interrupted`` and the interpreter recorded ``exit_code``.
    ``stop``: ``("worker_stopped" | "worker_lost", cause)``, the owner's note that IT stopped the turn.
    ``owner_gone``: the owner that admitted the attempt has exited (a restart whose old gateway died
    before writing that note); otherwise it is this live process. ``lost``: a plain ``worker_lost``
    reason (no stop cause), the owner's record of a worker it lost."""
    conn = board.conn
    worker_pid, bound_pid = _absent_pid(3_800_000 + 10 * n), _absent_pid(3_850_000 + 10 * n)
    lock = f"{HOST}:{_absent_pid(3_900_000 + 10 * n)}"
    run_id = int(conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, worker_pid, started_at) "
        "VALUES (?, 'fan', 'running', ?, ?, ?, ?)", (tid, lock, NOW + 900, worker_pid, NOW - 600)).lastrowid)
    if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone() is None:
        conn.execute("INSERT INTO tasks (id, title, assignee, status, created_at, priority) "
                     "VALUES (?, ?, 'fan', 'ready', ?, 5)", (tid, tid, NOW - 86400))
    # The card's next attempt keeps its row (and its failure count), as a real re-claim does.
    conn.execute(
        "UPDATE tasks SET status = 'running', claim_lock = ?, claim_expires = ?, worker_pid = ?, "
        "worker_started_at = ?, last_heartbeat_at = ?, current_run_id = ?, started_at = ? WHERE id = ?",
        (lock, NOW + 900, worker_pid, FINGERPRINT, NOW - 30, run_id, NOW - 600, tid))
    _event(conn, tid, run_id, "claimed", {"lock": lock, "expires": NOW + 900})
    request_id = f'kanban:["default","{tid}",{run_id}]'
    owner = {"pid": _absent_pid(3_950_000), "birth": 1.0} if owner_gone else LIVE_OWNER
    _event(conn, tid, run_id, "owner_admitted", {"db": str(board.ledger), "session_id": f"s-{tid}",
                                                 **owner, "request_id": request_id})
    with sqlite3.connect(board.ledger) as store:
        store.execute("INSERT INTO session_admissions VALUES (?, ?, 'terminal', 'interrupted')",
                      (f"s-{tid}", request_id))
    _event(conn, tid, run_id, "worker_bound", {"pid": bound_pid, "claim_lock": lock, "started_at": FINGERPRINT})
    _event(conn, tid, run_id, "worker_result", {"pid": bound_pid, "claim_lock": lock, "exit_code": exit_code,
                                                "last_output": ""})
    if stop is not None:
        kind, cause = stop
        payload = {"cause": cause} if kind == "worker_stopped" else {"reason": "EOFError", "stop_cause": cause}
        _event(conn, tid, run_id, kind, {**payload, "pid": bound_pid, "claim_lock": stop_lock or lock,
                                         "run_id": run_id})
    if lost is not None:
        _event(conn, tid, run_id, "worker_lost", {"reason": lost, "pid": bound_pid, "bound": True,
                                                  "claim_lock": lock, "run_id": run_id})
    conn.commit()
    return run_id


def _tick(board):
    return kbd.dispatch_once(board.conn, max_spawn=0, stale_timeout_seconds=0)


def _row(board, table, key):
    return dict(board.conn.execute(f"SELECT * FROM {table} WHERE id = ?", (key,)).fetchone())


@pytest.mark.parametrize("stop_kind", ["worker_stopped", "worker_lost"])
def test_attempts_the_gateway_stopped_for_its_restart_are_requeued_not_crashed(board, stop_kind):
    # Several at once, as a restart cuts every in-flight card: the systemic same-error rule must not
    # block them either.
    runs = {f"t_{i}": _running(board, f"t_{i}", i, exit_code=130, stop=(stop_kind, RESTARTING)) for i in range(4)}
    result = _tick(board)
    assert result.crashed == [] and result.auto_blocked == []
    for tid, run_id in runs.items():
        task, run = _row(board, "tasks", tid), _row(board, "task_runs", run_id)
        assert task["status"] == "ready"
        assert task["consecutive_failures"] == 0
        assert run["outcome"] == "reclaimed"
        assert RESTARTING in run["error"] and "without counting a failure" in run["error"]
        assert json.loads(run["metadata"])["gateway_stop"] == RESTARTING
        kinds = [r[0] for r in board.conn.execute("SELECT kind FROM task_events WHERE task_id = ?", (tid,))]
        assert "gave_up" not in kinds and "crashed" not in kinds and "reclaimed" in kinds


def test_a_real_nonzero_exit_still_counts_as_a_crash(board):
    runs = {f"t_{i}": _running(board, f"t_{i}", i, exit_code=1) for i in range(3)}
    result = _tick(board)
    assert sorted(result.crashed) == sorted(runs)
    for tid, run_id in runs.items():
        assert _row(board, "task_runs", run_id)["outcome"] == "crashed"
        assert _row(board, "tasks", tid)["consecutive_failures"] == 1
    # Three identical crashes in one tick is still the systemic trip, held for an operator.
    assert sorted(result.auto_blocked) == sorted(runs)
    assert all(kb._has_sticky_block(board.conn, tid) for tid in runs)


def _assert_requeued_free(board, tid, run_id, cause):
    task, run = _row(board, "tasks", tid), _row(board, "task_runs", run_id)
    assert (task["status"], task["consecutive_failures"], run["outcome"]) == ("ready", 0, "reclaimed")
    assert json.loads(run["metadata"])["gateway_stop"] == cause


def test_a_restart_whose_old_gateway_died_before_noting_its_stop_blocks_nothing(board):
    """Nov 14 00:08: the old gateway was killed mid-shutdown before it wrote ``worker_stopped``;
    the successor settled every orphaned card admission ``interrupted`` and the first sweep
    blocked 8 of them as one systemic crash. That settlement under a dead owner is the stop."""
    runs = {f"t_{i}": _running(board, f"t_{i}", i, exit_code=130, owner_gone=True) for i in range(11)}
    result = _tick(board)
    assert result.crashed == [] and result.auto_blocked == []
    for tid, run_id in runs.items():
        _assert_requeued_free(board, tid, run_id, OWNER_EXITED_CAUSE)
        assert not kb._has_sticky_block(board.conn, tid)


def test_a_mixed_sweep_counts_each_class_on_its_own(board):
    """Restart victims neither charge the breaker nor feed the same-error count: two real crashes
    with the very error text the victims used to carry stay ordinary crashes (below the systemic
    3), while three identical real crashes still trip sticky. A worker the owner LOST before it
    died is the attempt's own crash even though its admission also settled interrupted."""
    restarted = {f"t_r{i}": _running(board, f"t_r{i}", i, exit_code=130, owner_gone=True) for i in range(5)}
    same_text = {f"t_s{i}": _running(board, f"t_s{i}", 10 + i, exit_code=130) for i in range(2)}
    systemic = {f"t_x{i}": _running(board, f"t_x{i}", 20 + i, exit_code=1) for i in range(3)}
    lost = _running(board, "t_lost", 30, exit_code=137, owner_gone=True, lost="managed_worker_failed")
    result = _tick(board)
    for tid, run_id in restarted.items():
        _assert_requeued_free(board, tid, run_id, OWNER_EXITED_CAUSE)
    assert sorted(result.crashed) == sorted([*same_text, *systemic, "t_lost"])
    assert sorted(result.auto_blocked) == sorted(systemic)
    for tid in [*same_text, "t_lost"]:
        task = _row(board, "tasks", tid)
        assert (task["status"], task["consecutive_failures"]) == ("ready", 1)
    assert _row(board, "task_runs", lost)["outcome"] == "crashed"
    assert all(kb._has_sticky_block(board.conn, tid) for tid in systemic)


def test_an_owner_that_dies_under_a_cards_attempts_in_a_row_is_charged_not_systemic(board):
    """An owner death may be the attempt's own doing (an OOM, a crash in its turn), so only the
    first in a row is free. Three cards losing their owner twice in a row is the same-error count
    the systemic rule watches, yet each is an ordinary crash; a card that keeps doing it trips at
    its normal limit rather than looping free forever."""
    cards = [f"t_{i}" for i in range(3)]
    for i, tid in enumerate(cards):
        _running(board, tid, i, exit_code=130, owner_gone=True)
    _tick(board)
    second = {tid: _running(board, tid, 10 + i, exit_code=130, owner_gone=True) for i, tid in enumerate(cards)}
    result = _tick(board)
    assert sorted(result.crashed) == cards and result.auto_blocked == []
    for tid, run_id in second.items():
        task, run = _row(board, "tasks", tid), _row(board, "task_runs", run_id)
        assert (task["status"], task["consecutive_failures"], run["outcome"]) == ("ready", 1, "crashed")
        assert "exited mid-attempt again" in run["error"] and not kb._has_sticky_block(board.conn, tid)
    _running(board, "t_0", 20, exit_code=130, owner_gone=True)
    assert _tick(board).auto_blocked == ["t_0"]
    assert _row(board, "tasks", "t_0")["consecutive_failures"] == kbd.DEFAULT_FAILURE_LIMIT


def test_a_crash_under_the_live_successor_after_a_restart_still_counts(board):
    """The restart's mark belongs to the run the dead owner admitted. The card's next run, admitted
    by the live successor, crashing with the very same exit is the attempt's own crash."""
    _running(board, "t_next", 0, exit_code=130, owner_gone=True)
    _tick(board)
    run_id = _running(board, "t_next", 1, exit_code=130)
    _tick(board)
    assert _row(board, "task_runs", run_id)["outcome"] == "crashed"
    assert _row(board, "tasks", "t_next")["consecutive_failures"] == 1


def test_a_stop_note_from_another_claim_says_nothing_about_this_attempt(board):
    run_id = _running(board, "t_other", 0, exit_code=130, stop=("worker_stopped", RESTARTING),
                      stop_lock=f"{HOST}:1")
    _tick(board)
    assert _row(board, "task_runs", run_id)["outcome"] == "crashed"
    assert _row(board, "tasks", "t_other")["consecutive_failures"] == 1


def _policy(board, tid, run_id):
    task = _row(board, "tasks", tid)
    return SimpleNamespace(kanban_json=json.dumps({"task_id": tid, "db": str(kb.kanban_db_path()),
                                                   "run_id": run_id, "claim_lock": task["claim_lock"]}))


@pytest.mark.asyncio
async def test_the_owner_notes_only_a_turn_it_interrupted_for_a_restart(board):
    """The owner side: an interrupted Kanban turn whose worker the gateway stopped is noted under the
    run's claim; a user's Stop (no cause) or a turn that finished records nothing."""
    from gateway.session_managed_worker import _record_gateway_stop
    run_id = _running(board, "t_owner", 0, exit_code=130)
    policy, authority = _policy(board, "t_owner", run_id), SimpleNamespace()
    for accepted, cause in (({"interrupted": True}, None), ({"final_response": "done"}, RESTARTING),
                            ({"interrupted": True}, RESTARTING)):
        await _record_gateway_stop(authority, policy, accepted, SimpleNamespace(stop_cause=cause), {"pid": 7})
    await asyncio.gather(*getattr(authority, "_mutation_tasks", ()))
    notes = [json.loads(r[0]) for r in board.conn.execute(
        "SELECT payload FROM task_events WHERE task_id = 't_owner' AND kind = 'worker_stopped'")]
    assert [(n["cause"], n["claim_lock"], n["run_id"]) for n in notes] == [
        (RESTARTING, _row(board, "tasks", "t_owner")["claim_lock"], run_id)]
    _tick(board)
    assert _row(board, "task_runs", run_id)["outcome"] == "reclaimed"
