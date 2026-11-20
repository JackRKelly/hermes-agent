"""``kanban.disk_guard``: hold spawns on a nearly full disk; ENOSPC never gives up a card.

Regression for the Nov 19-20 outage: the worker host's Data volume sat at 0 bytes free for ~10
hours, the dispatcher kept claiming cards into it, each spawn failed with ``[Errno 28] No space
left on device`` (or ``database or disk is full`` / git's ``unable to create file``), two such
failures tripped the breaker, and 60+ cards ended ``blocked``/``gave_up`` until they were unblocked
by hand. The disk is faked (``statvfs`` seam) and the board is a real temp ``kanban.db``.
"""

from __future__ import annotations

import errno
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_disk_guard as kbdg

GB = kbdg.GB
SETTINGS = kbdg.DiskGuardSettings()
BUILDER = "crew-pilot-claude-builder"
REVIEWER = "crew-pilot-claude-reviewer"


class FakeDisk:
    """One volume whose free space the test sets; ``statvfs`` calls are counted."""

    def __init__(self, free_gb: float) -> None:
        self.free_gb = free_gb
        self.calls = 0
        self.guard: kbdg.DiskGuard  # set by the ``disk`` fixture

    def statvfs(self, path: str):
        self.calls += 1
        return SimpleNamespace(f_bavail=int(self.free_gb * GB), f_frsize=1)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbdg, "_GUARDS", {})
    monkeypatch.setattr(kbdg, "_ACTIVE", None)
    kb.init_db()
    return home


@pytest.fixture
def disk(kanban_home, monkeypatch):
    """The process guard for the test's kanban root, on a fake disk (100 GB free)."""
    fake = FakeDisk(100)
    guard = kbdg.DiskGuard(root=kb.kanban_home(), statvfs=fake.statvfs, device_of=lambda p: 1)
    kbdg._GUARDS[str(kb.kanban_home())] = guard
    fake.guard = guard
    return fake


def _set_free(disk: FakeDisk, gb: float) -> None:
    disk.free_gb = gb
    disk.guard._read_at = None  # past the cache window


@pytest.fixture
def spawns():
    return []


@pytest.fixture
def spawn_fn(spawns):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
    return fake_spawn


def _events(conn, tid: str, kind: str) -> list[dict]:
    return [json.loads(r[0]) if r[0] else {} for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind))]


def _task(conn, tid: str):
    return conn.execute("SELECT status, consecutive_failures FROM tasks WHERE id = ?", (tid,)).fetchone()


# --- thresholds and hysteresis -------------------------------------------------------------------

@pytest.mark.parametrize("free_gb, previous, expected", [
    (100, "ok", "ok"),
    (39.9, "ok", "heavy_paused"),
    (19.9, "ok", "all_paused"),
    (19.9, "heavy_paused", "all_paused"),
    # Leaving a state needs its line plus the 5 GB margin.
    (42, "heavy_paused", "heavy_paused"),
    (45, "heavy_paused", "ok"),
    (22, "all_paused", "all_paused"),
    (25, "all_paused", "heavy_paused"),  # past the all line, still under the heavy one
    (44.9, "all_paused", "heavy_paused"),
    (45, "all_paused", "ok"),
])
def test_classify_thresholds_and_hysteresis(free_gb, previous, expected):
    assert kbdg.classify(int(free_gb * GB), previous, SETTINGS) == expected


def test_lowest_volume_counts_and_each_volume_is_read_once(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    free = {str(a): 90 * GB, str(b): 30 * GB}
    calls: list[str] = []

    def statvfs(path):
        calls.append(path)
        return SimpleNamespace(f_bavail=free[path], f_frsize=1)

    guard = kbdg.DiskGuard(root=tmp_path, statvfs=statvfs, device_of=lambda p: hash(p))
    # A path not created yet is measured on its nearest existing parent's volume.
    assert guard.measure([str(a), str(b), str(a / "not-yet" / "wt")]) == 30 * GB
    assert calls == [str(a), str(b)]


def test_reading_is_cached_within_the_window(tmp_path):
    disk = FakeDisk(100)
    now = [1000.0]
    guard = kbdg.DiskGuard(root=tmp_path, statvfs=disk.statvfs, device_of=lambda p: 1, clock=lambda: now[0])
    paths = lambda: [str(tmp_path)]
    guard.evaluate(SETTINGS, paths)
    guard.evaluate(SETTINGS, paths)
    assert disk.calls == 1
    now[0] += kbdg.CACHE_SECONDS + 1
    disk.free_gb = 10
    assert guard.evaluate(SETTINGS, paths).state == "all_paused"
    assert disk.calls == 2


def test_parse_disk_guard_defaults_and_disabled():
    assert kbdg.parse_disk_guard(None) == SETTINGS
    assert kbdg.parse_disk_guard({}) == SETTINGS
    assert kbdg.parse_disk_guard({"enabled": False}) is None
    parsed = kbdg.parse_disk_guard({"heavy_min_free_gb": 60, "heavy_assignees": ["x-*"], "extra_paths": "/v"})
    assert parsed is not None
    assert (parsed.heavy_min_free_gb, parsed.heavy_assignees, parsed.extra_paths) == (60.0, ("x-*",), ("/v",))


# --- state file ----------------------------------------------------------------------------------

def test_transitions_write_state_file_and_log(tmp_path):
    disk = FakeDisk(100)
    guard = kbdg.DiskGuard(root=tmp_path, statvfs=disk.statvfs, device_of=lambda p: 1)
    paths = lambda: [str(tmp_path)]
    guard.evaluate(SETTINGS, paths)
    assert not (tmp_path / "state" / kbdg.STATE_FILE).exists()  # ok at start: nothing changed
    disk.free_gb, guard._read_at = 30, None
    guard.evaluate(SETTINGS, paths)
    guard._read_at = None
    guard.evaluate(SETTINGS, paths)  # same state: no second line
    state = json.loads((tmp_path / "state" / kbdg.STATE_FILE).read_text())
    assert state["state"] == "heavy_paused" and state["previous"] == "ok"
    assert state["free_bytes"] == 30 * GB and state["threshold_gb"] == 40
    assert isinstance(state["since"], int)
    disk.free_gb, guard._read_at = 50, None
    guard.evaluate(SETTINGS, paths)
    lines = (tmp_path / "state" / kbdg.LOG_FILE).read_text().splitlines()
    assert [json.loads(l)["state"] for l in lines] == ["heavy_paused", "ok"]


def test_state_write_survives_oserror_and_is_retried(tmp_path):
    (tmp_path / "state").write_text("not a dir")  # every write under state/ raises OSError
    disk = FakeDisk(10)
    guard = kbdg.DiskGuard(root=tmp_path, statvfs=disk.statvfs, device_of=lambda p: 1)
    paths = lambda: [str(tmp_path)]
    assert guard.evaluate(SETTINGS, paths).state == "all_paused"  # no raise
    (tmp_path / "state").unlink()
    guard._read_at = None
    guard.evaluate(SETTINGS, paths)
    assert json.loads((tmp_path / "state" / kbdg.STATE_FILE).read_text())["state"] == "all_paused"


def test_restart_mid_outage_keeps_state_and_episode(tmp_path):
    disk = FakeDisk(10)
    first = kbdg.DiskGuard(root=tmp_path, statvfs=disk.statvfs, device_of=lambda p: 1)
    first.evaluate(SETTINGS, lambda: [str(tmp_path)])
    second = kbdg.DiskGuard(root=tmp_path, statvfs=disk.statvfs, device_of=lambda p: 1)
    second.load_persisted()
    disk.free_gb = 22  # inside the margin: still all_paused because the state survived
    snap = second.evaluate(SETTINGS, lambda: [str(tmp_path)])
    assert snap.state == "all_paused" and snap.episode_since == first.episode_since
    assert len((tmp_path / "state" / kbdg.LOG_FILE).read_text().splitlines()) == 1


# --- dispatch holds ------------------------------------------------------------------------------

def test_heavy_paused_holds_builders_and_new_worktrees_only(kanban_home, disk, spawns, spawn_fn, tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(kbd._kbw, "_resolve_worktree_workspace",
                        lambda task, board=None: (Path(task.workspace_path), None))
    existing = tmp_path / "wt-existing"
    existing.mkdir()
    (existing / ".git").write_text("gitdir: elsewhere\n")
    with kbc.connect_closing() as conn:
        builder = kb.create_task(conn, title="build", assignee=BUILDER)
        new_tree = kb.create_task(conn, title="new tree", assignee=REVIEWER, workspace_kind="worktree",
                                  workspace_path=str(tmp_path / "wt-new"))
        old_tree = kb.create_task(conn, title="old tree", assignee=REVIEWER, workspace_kind="worktree",
                                  workspace_path=str(existing))
        light = kb.create_task(conn, title="light", assignee=REVIEWER)
        assert not kbdg.is_heavy(conn, old_tree, REVIEWER, SETTINGS)
        _set_free(disk, 30)
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, disk_guard=SETTINGS)
    assert res.disk_guard == "heavy_paused"
    assert sorted(spawns) == sorted([old_tree, light])
    assert sorted(t for t, r in res.respawn_guarded if r == "disk_low") == sorted([builder, new_tree])


def test_all_paused_claims_nothing_on_any_lane(kanban_home, disk, spawns, spawn_fn):
    with kbc.connect_closing() as conn:
        ready = kb.create_task(conn, title="light", assignee=REVIEWER)
        review = kb.create_task(conn, title="review", assignee=REVIEWER)
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review,))
        conn.commit()
        _set_free(disk, 5)
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, disk_guard=SETTINGS)
        assert spawns == []
        assert res.disk_guard == "all_paused"
        assert {t for t, r in res.respawn_guarded if r == "disk_low"} == {ready, review}
        assert _task(conn, ready)[0] == "ready" and _task(conn, review)[0] == "review"
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind = 'claimed'").fetchone()[0] == 0


def test_one_disk_low_event_per_episode(kanban_home, disk, spawn_fn):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="build", assignee=BUILDER)
        _set_free(disk, 30)
        for _ in range(3):
            _set_free(disk, 30)
            kbd.dispatch_once(conn, spawn_fn=spawn_fn, disk_guard=SETTINGS)
        _set_free(disk, 10)  # heavy -> all stays one episode
        kbd.dispatch_once(conn, spawn_fn=spawn_fn, disk_guard=SETTINGS)
        events = _events(conn, tid, "respawn_guarded")
        assert len(events) == 1
        assert events[0] == {"reason": "disk_low", "free_gb": 30.0, "state": "heavy_paused"}
        # Back to ok, then low again: a new episode, one more event.
        conn.execute("UPDATE task_events SET created_at = created_at - 10 WHERE task_id = ?", (tid,))
        conn.commit()
        _set_free(disk, 100)
        assert disk.guard.evaluate(SETTINGS, lambda: [str(kb.kanban_home())]).state == "ok"
        _set_free(disk, 30)
        kbd.dispatch_once(conn, spawn_fn=spawn_fn, disk_guard=SETTINGS)
        assert len(_events(conn, tid, "respawn_guarded")) == 2


def test_guard_disabled_dispatches_as_before(kanban_home, disk, spawns, spawn_fn):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="build", assignee=BUILDER)
        _set_free(disk, 0)
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, disk_guard=kbdg.parse_disk_guard({"enabled": False}))
    assert spawns == [tid] and res.disk_guard is None and disk.calls == 0


# --- ENOSPC is infrastructure --------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (OSError(errno.ENOSPC, "No space left on device"), True),
    ("[Errno 28] No space left on device: '/x/kanban/logs/t.log'", True),
    ("sqlite3.OperationalError: database or disk is full", True),
    ("git worktree add failed for /x on branch b: error: unable to create file a.ts", True),
    ("git worktree add failed for /x on branch b: error: unable to write file a.ts", True),
    ("error: unable to create file a.ts: Permission denied", False),  # no worktree add: ambiguous
    ("sqlite3.OperationalError: disk I/O error", False),
    ("boom", False),
    (None, False),
])
def test_is_disk_full_error(value, expected):
    assert kbdg.is_disk_full_error(value) is expected


def test_is_disk_full_error_follows_the_cause_chain():
    try:
        try:
            raise OSError(errno.ENOSPC, "full")
        except OSError as inner:
            raise RuntimeError("spawn failed") from inner
    except RuntimeError as outer:
        assert kbdg.is_disk_full_error(outer)


def _enospc_spawn(task, workspace, board=None):
    raise OSError(errno.ENOSPC, "No space left on device", f"/x/kanban/logs/{task.id}.log")


def _fail_spawn(tid_spawn, conn, tid, n: int, *, guard, disk) -> None:
    for _ in range(n):
        _set_free(disk, 100)
        kbd.dispatch_once(conn, spawn_fn=tid_spawn, disk_guard=guard)
        # Past infrastructure_cooldown so the next tick tries again.
        conn.execute("UPDATE task_runs SET ended_at = ended_at - 3600 WHERE task_id = ?", (tid,))
        conn.commit()


def test_enospc_spawn_failure_never_counts_or_gives_up(kanban_home, disk):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="build", assignee=BUILDER)
        _fail_spawn(_enospc_spawn, conn, tid, 4, guard=SETTINGS, disk=disk)
        assert tuple(_task(conn, tid)) == ("ready", 0)
        assert _events(conn, tid, "gave_up") == []
        failed = _events(conn, tid, "spawn_failed")
        assert len(failed) == 4 and all(e.get("infrastructure") for e in failed)


def test_enospc_workspace_failure_never_counts_or_gives_up(kanban_home, disk, monkeypatch):
    def full_tree(task, board=None):
        raise RuntimeError(f"git worktree add failed for /wt/{task.id} on branch b: error: unable to create file x")
    monkeypatch.setattr(kbd._kbw, "_resolve_worktree_workspace", full_tree)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="tree", assignee=REVIEWER, workspace_kind="worktree")
        _fail_spawn(lambda *a, **k: None, conn, tid, 3, guard=SETTINGS, disk=disk)
        assert tuple(_task(conn, tid)) == ("ready", 0)
        assert _events(conn, tid, "gave_up") == []


def test_non_enospc_spawn_failure_still_trips(kanban_home, disk):
    def boom(task, workspace, board=None):
        raise RuntimeError("boom")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="build", assignee=BUILDER)
        _fail_spawn(boom, conn, tid, 2, guard=SETTINGS, disk=disk)
        assert tuple(_task(conn, tid)) == ("blocked", 2)
        assert len(_events(conn, tid, "gave_up")) == 1


def test_enospc_with_guard_disabled_trips_as_before(kanban_home, disk):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="build", assignee=BUILDER)
        _fail_spawn(_enospc_spawn, conn, tid, 2, guard=None, disk=disk)
        assert tuple(_task(conn, tid)) == ("blocked", 2)


def _crash_once(conn, tid: str, log_text: str) -> None:
    log_path = kb.worker_log_path(tid)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log_text)
    host_prefix = kb._claimer_id().split(":", 1)[0]
    assert kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock") is not None
    kbd._set_worker_pid(conn, tid, 991101)
    kbd._record_worker_exit(991101, 1)
    kbd.detect_crashed_workers(conn)


@pytest.mark.parametrize("free_gb, log_text", [
    (100, "Traceback ...\nOSError: [Errno 28] No space left on device\n"),  # output shows a full disk
    (30, "Traceback ...\nRuntimeError: something odd\n"),                    # died while the disk was low
])
def test_crash_on_full_disk_never_counts_or_gives_up(kanban_home, disk, monkeypatch, free_gb, log_text):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="build", assignee=REVIEWER)
        _set_free(disk, free_gb)
        kbdg.begin_tick(conn, SETTINGS, dry_run=False)
        for _ in range(3):
            _crash_once(conn, tid, log_text)
        assert tuple(_task(conn, tid)) == ("ready", 0)
        assert _events(conn, tid, "gave_up") == []
        run = conn.execute("SELECT outcome, metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                           (tid,)).fetchone()
        assert run[0] == "crashed" and json.loads(run[1])["infrastructure"] is True
        assert kbd.check_respawn_guard(conn, tid) == "infrastructure_cooldown"


def test_ordinary_crash_still_trips(kanban_home, disk, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="build", assignee=REVIEWER)
        kbdg.begin_tick(conn, SETTINGS, dry_run=False)
        for _ in range(2):
            _crash_once(conn, tid, "RuntimeError: something odd\n")
        assert tuple(_task(conn, tid)) == ("blocked", 2)


# --- recovery ------------------------------------------------------------------------------------

def _park_for_disk(conn, disk, assignee: str = BUILDER, spawn=_enospc_spawn) -> str:
    """A card the pre-guard dispatcher parked: two ENOSPC spawn failures with the guard off."""
    tid = kb.create_task(conn, title="parked", assignee=assignee)
    _fail_spawn(spawn, conn, tid, 2, guard=None, disk=disk)
    assert _task(conn, tid)[0] == "blocked"
    return tid


def test_recovery_unblocks_disk_gave_up_cards_once(kanban_home, disk, monkeypatch):
    monkeypatch.setattr(kbdg, "_profile_exists", lambda: (lambda name: name in {"main-captain"}))
    with kbc.connect_closing() as conn:
        parked = _park_for_disk(conn, disk)
        agent_noted = _park_for_disk(conn, disk)
        kb.add_comment(conn, agent_noted, "main-captain", "seen, disk is full")  # an agent, not a person
        other = _park_for_disk(conn, disk, spawn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        human = _park_for_disk(conn, disk)
        kb.add_comment(conn, human, "jack", "leave this one")
        marked = _park_for_disk(conn, disk)
        with kb.write_txn(conn):
            kb._append_event(conn, marked, "blocked", {"reason": "[needs-jack] decide first"})

        recovered = kbdg.recover_disk_gave_up(conn)
        assert sorted(recovered) == sorted([parked, agent_noted])
        assert tuple(_task(conn, parked)) == ("ready", 0)
        assert _events(conn, parked, "unblocked")[-1]["reason"] == "disk_recovered"
        for tid in (other, human, marked):
            assert _task(conn, tid)[0] == "blocked"
        # Once: a second pass finds nothing new, and a re-block by someone stays put.
        assert kbdg.recover_disk_gave_up(conn) == []


def test_recovery_runs_once_per_ok_epoch_from_the_tick(kanban_home, disk, spawn_fn):
    with kbc.connect_closing() as conn:
        tid = _park_for_disk(conn, disk)
        # A fresh process (no reading yet) starting with the disk ok runs one recovery pass.
        disk.guard = kbdg._GUARDS[str(kb.kanban_home())] = kbdg.DiskGuard(
            root=kb.kanban_home(), statvfs=disk.statvfs, device_of=lambda p: 1)
        kbd.dispatch_once(conn, spawn_fn=spawn_fn, disk_guard=SETTINGS)
        assert _task(conn, tid)[0] in ("ready", "running")
        assert len(_events(conn, tid, "unblocked")) == 1
        # Later ok ticks in the same epoch do not run it again.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'blocked', claim_lock = NULL WHERE id = ?", (tid,))
        _set_free(disk, 100)
        kbd.dispatch_once(conn, spawn_fn=spawn_fn, disk_guard=SETTINGS)
        assert len(_events(conn, tid, "unblocked")) == 1


def test_recovery_skips_runs_older_than_the_window(kanban_home, disk):
    with kbc.connect_closing() as conn:
        tid = _park_for_disk(conn, disk)
        recovered = kbdg.recover_disk_gave_up(conn, now=time.time() + 8 * 86400)
        assert recovered == [] and _task(conn, tid)[0] == "blocked"


def test_recovery_is_bounded_per_pass(kanban_home, disk):
    with kbc.connect_closing() as conn:
        ids = [_park_for_disk(conn, disk) for _ in range(3)]
        assert len(kbdg.recover_disk_gave_up(conn, limit=2)) == 2
        assert len(kbdg.recover_disk_gave_up(conn, limit=2)) == 1
        assert all(_task(conn, t)[0] == "ready" for t in ids)
