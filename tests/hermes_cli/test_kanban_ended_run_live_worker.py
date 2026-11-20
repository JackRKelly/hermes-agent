"""Invariant: one worker per card. A run ended from OUTSIDE its worker (the autopilot's
rejection cap, the block-loop breaker routing to triage) must not leave that worker running
beside the card's next run (t_efcda9e3, Nov 19).

What happened: the autopilot blocked a card the dispatcher had just claimed, so the run ended
while its managed interpreter kept working. The terminal-worker reaper killed the run's
submitter 120 s later and cleared the run row's pid in the same step. The interpreter's
``_watch_submitter`` re-read that row, saw NULL ("no submitter yet") and never exited. Four
minutes later a second run spawned on the same worktree, and two writers ran on one branch.

The fixes:
  * ``_watch_submitter`` latches the submitter it first saw, so a cleared row cannot hide the kill,
    exits with its whole process group, and ends itself when its run has been over for the reaper's
    grace with no submitter ever recorded (nothing else could find it then);
  * the respawn guard holds a card while an ended run's verified, host-local worker is alive: the
    submitter AND the bound interpreter, which outlives the reaper's clearing of the run row by
    up to a watcher tick.
"""

from __future__ import annotations

import os
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


def _claimed_card(conn) -> tuple[str, int]:
    """A claimed card whose run has no submitter recorded yet."""
    tid = kb.create_task(conn, title="builder card", assignee="coder")
    kb.claim_task(conn, tid, claimer=kb._claimer_id())
    run_id = kb._current_run_id(conn, tid)
    assert run_id is not None
    return tid, run_id


def _running_card(conn) -> tuple[str, int, subprocess.Popen]:
    """A claimed card whose run has a spawned, fingerprinted submitter (the dispatcher's worker)."""
    tid, run_id = _claimed_card(conn)
    submitter = _sleeper()
    kbd._set_worker_pid(conn, tid, submitter.pid)
    return tid, run_id, submitter


def _record_bound(conn, tid: str, run_id: int, pid: int) -> None:
    """The ``worker_bound`` event ``bind_worker_context`` writes for the run's interpreter."""
    lock = conn.execute("SELECT claim_lock FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0]
    fingerprint = kbd._process_fingerprint(pid)
    assert fingerprint, "the test host must fingerprint a live child"
    kb._append_event(conn, tid, "worker_bound",
                     {"pid": pid, "claim_lock": lock, "started_at": fingerprint}, run_id=run_id)


def _bind_interpreter(conn, db_path: Path, tid: str, run_id: int, *, script: str = _INTERPRETER,
                      new_session: bool = False, expect_alive: bool = True) -> subprocess.Popen:
    interpreter = subprocess.Popen(
        [sys.executable, "-c", script, str(db_path), str(run_id)], cwd=REPO,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=new_session,
    )
    time.sleep(0.2)
    _record_bound(conn, tid, run_id, interpreter.pid)
    time.sleep(2.3)  # the watcher reads the run row once a second: let it see the submitter
    if expect_alive:
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
    interpreter = _bind_interpreter(conn, db_path, tid, run_id)
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
    interpreter = _bind_interpreter(conn, db_path, tid, run_id)
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
    The dispatcher must not start a second run beside the first run's live worker. The reaper
    then kills the submitter and clears the run row in the same dispatch tick that would claim
    the card, while the bound interpreter is still alive (its watcher has not ticked yet; here it
    is a plain process standing in for that moment, so the window is deterministic). The card
    stays held until the interpreter itself has exited, and only then spawns."""
    conn, _ = board
    tid, run_id, submitter = _running_card(conn)
    interpreter = _sleeper()  # the bound interpreter, frozen before its watcher's next tick
    try:
        _record_bound(conn, tid, run_id, interpreter.pid)
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
        assert conn.execute("SELECT worker_pid FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0] is None
        assert interpreter.poll() is None

        assert kbd.check_respawn_guard(conn, tid) == "previous_worker_alive", \
            "the card was released beside the ended run's live interpreter"
        assert tid not in [s[0] for s in kbd.dispatch_once(conn, dry_run=True).spawned]

        _stop(interpreter)
        assert kbd.check_respawn_guard(conn, tid) is None
        assert tid in [s[0] for s in kbd.dispatch_once(conn, dry_run=True).spawned]
    finally:
        _stop(submitter, interpreter)


def test_the_bound_interpreter_alone_holds_the_card(board):
    """A run blocked from outside before ``_set_worker_pid`` recorded its submitter: no pid ever
    reached the run row, so only the ``worker_bound`` event names the live writer."""
    conn, _ = board
    tid, run_id = _claimed_card(conn)
    interpreter = _sleeper()
    try:
        _record_bound(conn, tid, run_id, interpreter.pid)
        _block_from_outside(conn, tid)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))

        assert kbd.check_respawn_guard(conn, tid) == "previous_worker_alive"
        _stop(interpreter)
        assert kbd.check_respawn_guard(conn, tid) is None
    finally:
        _stop(interpreter)


def test_interpreter_with_no_recorded_submitter_ends_after_the_reapers_grace(board):
    """The same never-recorded submitter, seen from the interpreter: nothing can latch and no
    reaper can find it, so its watcher ends it once the run has been over for the reaper's grace,
    and not before (a worker finishing after its own transition keeps that grace)."""
    conn, db_path = board
    tid, run_id = _claimed_card(conn)
    interpreter = _bind_interpreter(conn, db_path, tid, run_id)
    try:
        _block_from_outside(conn, tid)
        assert not _exited_within(interpreter, 2.5), "a run ended inside the grace must not end its worker"

        conn.execute("UPDATE task_runs SET ended_at = ended_at - ? WHERE id = ?",
                     (kbd.TERMINAL_WORKER_REAP_GRACE_SECONDS + 1, run_id))
        assert _exited_within(interpreter, 5), "the interpreter outlived its long-ended run"
    finally:
        _stop(interpreter)


# An interpreter that leads its process group (as the owner spawns it) with a tool child inside it:
# the child's pid goes to stdout so the test can check it went down with the interpreter. Its sleep
# is short because a failing run orphans it outside the test's subtree, where conftest forbids kills.
_INTERPRETER_WITH_CHILD = (
    "import subprocess, sys, time\n"
    "from pathlib import Path\n"
    "from gateway.session_kanban import _watch_submitter\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
    "print(child.pid, flush=True)\n"
    "_watch_submitter(Path(sys.argv[1]), int(sys.argv[2]))\n"
    "time.sleep(120)\n"
)


def test_interpreter_takes_its_process_group_down_with_it(board):
    """A tool child in the interpreter's group (a git push mid-flight) must not keep writing after
    the attempt was ended from outside."""
    conn, db_path = board
    tid, run_id, submitter = _running_card(conn)
    interpreter = _bind_interpreter(conn, db_path, tid, run_id, script=_INTERPRETER_WITH_CHILD,
                                    new_session=True)
    assert interpreter.stdout is not None
    child_pid = int(interpreter.stdout.readline())
    try:
        assert kb._pid_alive(child_pid)
        submitter.kill()
        submitter.wait()

        assert _exited_within(interpreter, 5), "the interpreter outlived its killed submitter"
        deadline = time.monotonic() + 3
        while kb._pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not kb._pid_alive(child_pid), "the interpreter's tool child outlived it"
    finally:
        _stop(submitter, interpreter)
        interpreter.stdout.close()


def test_this_process_never_holds_a_card(board):
    """The reaper never acts on its own pid (it never clears such a row), so the guard must not
    hold on it either, or the hold would never lift."""
    conn, _ = board
    tid, run_id = _claimed_card(conn)
    kbd._set_worker_pid(conn, tid, os.getpid())
    _record_bound(conn, tid, run_id, os.getpid())
    kb.block_task(conn, tid, reason="parked from outside", kind="needs_input")
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))

    assert kbd.check_respawn_guard(conn, tid) is None


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
