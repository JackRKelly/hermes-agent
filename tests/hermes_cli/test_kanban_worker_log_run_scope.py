"""The dead-worker sweep books a run from what THAT run wrote to the append-mode worker log.

A worker killed before its first write (SIGKILL, a process-tree teardown) used to be booked
with the previous run's ``[kanban-worker-exit] rc=N`` trailer and its last output: live,
t_b130f886 run 3017 died without touching its log and was booked ``rc=1`` with run 2985's
"Kanban owner refused" text, which spent the card's failure budget on a phantom error.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_worker_log import WORKER_START_MARKER, run_scoped_log_tail
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

_PRIOR_OUTPUT = "Kanban owner refused: Kanban execution unknown; retained by owner"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    return home


def _spawn_dead_worker(conn, tid: str, pid: int, written: str = "") -> int:
    """Claim ``tid``, open its log the way the dispatcher's spawn does, let the worker write
    ``written`` and die unreaped by this process. Returns the claimed run id."""
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    conn.execute(
        "UPDATE tasks SET worker_pid=?, worker_started_at=NULL, started_at=? WHERE id=?",
        (pid, int(time.time()) - 120, tid),
    )
    conn.commit()
    task = kb.get_task(conn, tid)
    log_f = kbd._open_worker_log(task, None)
    log_f.write(written.encode("utf-8"))
    log_f.close()
    return task.current_run_id


def _last_run(conn, tid):
    run = conn.execute(
        "SELECT id, outcome, error, metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()
    return run, kb._json_dict(run["metadata"])


def test_run_that_wrote_nothing_is_not_booked_with_the_prior_runs_trailer(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        first = _spawn_dead_worker(conn, tid, 70001, f"Session: local-1\n{_PRIOR_OUTPUT}\n\n{KANBAN_WORKER_EXIT_TRAILER}1\n")
        kbd.detect_crashed_workers(conn)
        run, meta = _last_run(conn, tid)
        assert run["id"] == first
        assert meta.get("exit_code") == 1 and _PRIOR_OUTPUT in meta.get("worker_output", "")

        second = _spawn_dead_worker(conn, tid, 70002)  # killed before its first write
        kbd.detect_crashed_workers(conn)
        run, meta = _last_run(conn, tid)
        assert run["id"] == second != first
        assert run["outcome"] == "crashed"
        assert "pid 70002 not alive" in run["error"]
        assert _PRIOR_OUTPUT not in run["error"]
        assert "exit_code" not in meta and "exit_kind" not in meta
        assert "worker_output" not in meta


def test_single_run_with_marker_still_books_its_trailer(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _spawn_dead_worker(conn, tid, 70003, f"I did the work.\n\n{KANBAN_WORKER_EXIT_TRAILER}0\n")
        kbd.detect_crashed_workers(conn)
        run, meta = _last_run(conn, tid)
        assert meta.get("protocol_violation") is True and meta.get("exit_code") == 0
        assert meta.get("worker_output") == "I did the work."
        assert WORKER_START_MARKER not in run["error"]


def test_legacy_log_without_markers_reads_last_trailer_as_before(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        host = kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, tid, claimer=f"{host}:w70004")
        conn.execute("UPDATE tasks SET worker_pid=70004, worker_started_at=NULL, started_at=? WHERE id=?",
                     (int(time.time()) - 120, tid))
        conn.commit()
        log = kb.worker_log_path(tid)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"old run\n{KANBAN_WORKER_EXIT_TRAILER}1\nnewer run says hi\n{KANBAN_WORKER_EXIT_TRAILER}0\n",
                       encoding="utf-8")
        kbd.detect_crashed_workers(conn)
        _, meta = _last_run(conn, tid)
        assert meta.get("protocol_violation") is True and meta.get("exit_code") == 0


@pytest.mark.parametrize("raw, run_id, expected", [
    (None, 5, ""),
    ("legacy text\n", 5, "legacy text\n"),  # no marker: read whole, as before
    (f"a\n{WORKER_START_MARKER}4\nold\n{WORKER_START_MARKER}5\nnew\n", 5, "\nnew\n"),
    (f"a\n{WORKER_START_MARKER}4\nold\n", 5, ""),  # booked run never stamped this tail
    (f"a\n{WORKER_START_MARKER}4\nold\n{WORKER_START_MARKER}5\n", None, "\n"),
])
def test_run_scoped_log_tail(raw, run_id, expected):
    assert run_scoped_log_tail(raw, run_id) == expected
