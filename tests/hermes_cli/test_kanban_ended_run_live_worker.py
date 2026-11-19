"""Invariant: one worker per card. A run ended from OUTSIDE its worker (the autopilot's
rejection cap, the block-loop breaker routing to triage) must not leave that worker running
beside the card's next run (t_efcda9e3, Nov 19).

What happened: the autopilot blocked a card the dispatcher had just claimed, so the run ended
while its managed interpreter kept working. The terminal-worker reaper killed the run's
submitter 120 s later and cleared the run row's pid in the same step. The interpreter's
``_watch_submitter`` re-read that row, saw NULL ("no submitter yet") and never exited. Four
minutes later a second run spawned on the same worktree, and two writers ran on one branch.

Two fixes, two tests each:
  * ``_watch_submitter`` latches the submitter it first saw, so a cleared row cannot hide the kill;
  * the respawn guard holds a card while an ended run's verified, host-local worker is alive.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

REPO = Path(__file__).resolve().parents[2]

# The bound interpreter: a process that runs only the production watcher, then idles. Its one job
# in these tests is to exit when the submitter it watches dies, as a real managed worker must.
_INTERPRETER = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "from gateway.session_kanban import _watch_submitter\n"
    "_watch_submitter(Path(sys.argv[1]), int(sys.argv[2]))\n"
    "time.sleep(120)\n"
)


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c, db_path


def _sleeper():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.2)
    return proc


def _running_card(conn) -> tuple[str, int, subprocess.Popen]:
    """A claimed card whose run has a spawned, fingerprinted submitter (the dispatcher's worker)."""
    tid = kb.create_task(conn, title="builder card", assignee="coder")
    kb.claim_task(conn, tid, claimer=kb._claimer_id())
    run_id = kb._current_run_id(conn, tid)
    assert run_id is not None
    submitter = _sleeper()
    kbd._set_worker_pid(conn, tid, submitter.pid)
    return tid, run_id, submitter


def _bind_interpreter(db_path: Path, run_id: int) -> subprocess.Popen:
    interpreter = subprocess.Popen(
        [sys.executable, "-c", _INTERPRETER, str(db_path), str(run_id)], cwd=REPO,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2.5)  # the watcher reads the run row once a second: let it see the submitter
    assert interpreter.poll() is None, "the interpreter must run while its submitter lives"
    return interpreter


def _exited_within(proc: subprocess.Popen, seconds: float) -> bool:
    try:
        proc.wait(timeout=seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


def _status(conn, tid: str) -> str:
    task = kb.get_task(conn, tid)
    assert task is not None
    return task.status


def _block_from_outside(conn, tid: str) -> None:
    """The autopilot's rejection-cap park, written while the worker still runs: the breaker
    counts it as the third same-kind block and routes the card to triage."""
    conn.execute("UPDATE tasks SET block_kind = 'needs_input', block_recurrences = 2 WHERE id = ?", (tid,))
    assert kb.block_task(conn, tid, reason="[needs-attention] 3 rejected heads", kind="needs_input") is True
    assert _status(conn, tid) == "triage"


def _stop(*procs) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_interpreter_exits_when_its_submitter_dies_after_the_row_was_cleared(board):
    """Deterministic shape of the race: the run row's pid is cleared before the watcher's next
    read sees the dead submitter. Without the latch the watcher reads NULL forever and lives on."""
    conn, db_path = board
    tid, run_id, submitter = _running_card(conn)
    interpreter = _bind_interpreter(db_path, run_id)
    try:
        conn.execute("UPDATE task_runs SET worker_pid = NULL, worker_started_at = NULL WHERE id = ?", (run_id,))
        submitter.kill()
        submitter.wait()

        assert _exited_within(interpreter, 5), "the interpreter outlived its killed submitter"
    finally:
        _stop(submitter, interpreter)


def test_breaker_block_then_reap_ends_the_interpreter(board):
    """The Nov 19 sequence end to end: an outside block ends the run, the reaper kills the
    submitter after its grace and clears the row, and the bound interpreter must go with it."""
    conn, db_path = board
    tid, run_id, submitter = _running_card(conn)
    interpreter = _bind_interpreter(db_path, run_id)
    try:
        _block_from_outside(conn, tid)
        conn.execute("UPDATE task_runs SET ended_at = ended_at - ? WHERE id = ?",
                     (kbd.TERMINAL_WORKER_REAP_GRACE_SECONDS + 1, run_id))

        assert kbd.reap_terminal_workers(conn) == [tid]

        assert _exited_within(interpreter, 5), "the interpreter outlived its reaped submitter"
    finally:
        _stop(submitter, interpreter)


def test_card_is_not_respawned_while_the_ended_runs_worker_lives(board, all_assignees_spawnable):
    """Before the reaper's grace passes, the card is re-readied (Captain's note, triage -> ready).
    The dispatcher must not start a second run beside the first run's live worker; once that
    worker is reaped, the card spawns normally."""
    conn, _ = board
    tid, run_id, submitter = _running_card(conn)
    try:
        _block_from_outside(conn, tid)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))

        assert kbd.check_respawn_guard(conn, tid) == "previous_worker_alive"
        assert kbd.check_respawn_guard(conn, tid, lane="review") == "previous_worker_alive"
        held = kbd.dispatch_once(conn, dry_run=True)
        assert (tid, "previous_worker_alive") in held.respawn_guarded
        assert tid not in [s[0] for s in held.spawned]

        conn.execute("UPDATE task_runs SET ended_at = ended_at - ? WHERE id = ?",
                     (kbd.TERMINAL_WORKER_REAP_GRACE_SECONDS + 1, run_id))
        assert kbd.reap_terminal_workers(conn) == [tid]
        assert kbd.check_respawn_guard(conn, tid) is None
        assert tid in [s[0] for s in kbd.dispatch_once(conn, dry_run=True).spawned]
    finally:
        _stop(submitter)


@pytest.mark.parametrize("evidence", ["unverified", "foreign_host", "recycled"])
def test_unprovable_worker_evidence_never_holds_a_card(board, evidence):
    """The guard holds a card only on a worker the reaper itself would recognise and end:
    otherwise nothing ever lifts the hold and the card sits in ready forever."""
    conn, _ = board
    tid, run_id, submitter = _running_card(conn)
    try:
        kb.block_task(conn, tid, reason="parked from outside", kind="needs_input")
        if evidence == "unverified":
            conn.execute("UPDATE task_runs SET worker_started_at = ? WHERE id = ?",
                         (kbd.UNVERIFIED_WORKER_FINGERPRINT, run_id))
        elif evidence == "foreign_host":
            conn.execute("UPDATE task_runs SET claim_lock = 'other-host:1' WHERE id = ?", (run_id,))
        else:
            conn.execute("UPDATE task_runs SET worker_started_at = worker_started_at || '0' WHERE id = ?",
                         (run_id,))
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))

        assert kbd.check_respawn_guard(conn, tid) is None
    finally:
        _stop(submitter)
