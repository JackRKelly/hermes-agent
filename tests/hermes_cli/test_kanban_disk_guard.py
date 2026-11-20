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


def test_unmeasurable_disk_fails_open_and_keeps_hysteresis(kanban_home, disk, spawns, spawn_fn, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="build", assignee=BUILDER)
        _set_free(disk, 30)
        assert kbd.dispatch_once(conn, spawn_fn=spawn_fn, disk_guard=SETTINGS).disk_guard == "heavy_paused"
        assert spawns == []

        def refused(path):
            raise OSError(errno.EIO, "statvfs refused")
        monkeypatch.setattr(disk.guard, "statvfs", refused)
        disk.guard._read_at = None
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, disk_guard=SETTINGS)
        assert res.disk_guard == "ok" and spawns == [tid]  # a broken probe never holds the board
        assert disk.guard.state == "heavy_paused"           # ...but the recorded state stands
        # Measurable again inside the resume margin: still held, the hysteresis survived.
        monkeypatch.setattr(disk.guard, "statvfs", disk.statvfs)
        _set_free(disk, 42)
        assert disk.guard.evaluate(SETTINGS, lambda: [str(kb.kanban_home())]).state == "heavy_paused"


def test_standalone_daemon_is_guarded(kanban_home, disk, spawns, monkeypatch):
    import threading
    monkeypatch.setattr(kbd, "_default_spawn", lambda task, workspace, board=None: spawns.append(task.id))
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="review", assignee=REVIEWER)
    _set_free(disk, 10)
    stop, ticks = threading.Event(), []

    def on_tick(res):
        ticks.append(res)
        stop.set()
    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)
    assert ticks[0].disk_guard == "all_paused"
    assert (tid, "disk_low") in ticks[0].respawn_guarded and spawns == []


def test_guard_disabled_dispatches_as_before(kanban_home, disk, spawns, spawn_fn):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="build", assignee=BUILDER)
        _set_free(disk, 0)
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, disk_guard=kbdg.parse_disk_guard({"enabled": False}))
    assert spawns == [tid] and res.disk_guard is None and disk.calls == 0


# --- ENOSPC is infrastructure --------------------------------------------------------------------

# Stored run errors from the live board during the Nov 19-20 outage, verbatim (read-only query of
# ``task_runs.error`` / ``task_events.payload``), then the near misses that must stay card failures.
_WT = "workspace: git worktree add failed for /Users/jack/Code/Cybership/v2/.worktrees/{b} on branch crew/{b}: "
INCIDENT_DISK_FULL = [
    pytest.param(  # t_c010604c: the ref lock file, with the reason
        _WT.format(b="test-team-billing-restore")
        + "Preparing worktree (new branch 'crew/test-team-billing-restore')\nfatal: cannot lock ref "
        "'refs/heads/crew/test-team-billing-restore': Unable to create '/Users/jack/Code/Cybership/v2/.git/"
        "refs/heads/crew/test-team-billing-restore.lock': No space left on device", id="t_c010604c-lock-ref"),
    pytest.param(  # t_053e0026: the worker log
        "[Errno 28] No space left on device: '/Users/jack/.hermes/kanban/logs/t_053e0026.log'", id="t_053e0026-errno"),
    pytest.param(  # t_37089d95: the scratch workspace
        "workspace: [Errno 28] No space left on device: '/Users/jack/.hermes/kanban/workspaces/t_37089d95'",
        id="t_37089d95-workspace"),
    pytest.param(  # git checkout files, ENOSPC in its stderr
        _WT.format(b="bukuship-manifest-no-retry")
        + "Preparing worktree (new branch 'crew/bukuship-manifest-no-retry')\nerror: unable to create file "
        ".gitignore: No space left on device\nfatal: cannot create directory at '.greptile': No space left on "
        "device", id="worktree-create-file"),
    pytest.param(  # the 500-char cap cut the reason off
        _WT.format(b="returns-warehouse-access")
        + "Preparing worktree (new branch 'crew/returns-warehouse-access')\nerror: unable to write file "
        "apps/posts/linkedin-posts/20260624t020000z-001-flexible/post.md", id="worktree-write-file-truncated"),
    pytest.param(  # t_3615c4c7: git's second attempt no longer said why
        _WT.format(b="runner-mirror-followups")
        + "Preparing worktree (new branch 'crew/runner-mirror-followups')\nfatal: cannot update ref "
        "'refs/heads/crew/runner-mirror-followups': couldn't write '/Users/jack/Code/Cybership/v2/.git/refs/"
        "heads/crew/runner-mirror-followups.lock'", id="t_3615c4c7-couldnt-write-lock"),
    pytest.param(  # t_f21fd58f
        _WT.format(b="ci-tune-postgres-wait")
        + "Preparing worktree (new branch 'crew/ci-tune-postgres-wait')\nfatal: couldn't set "
        "'refs/heads/crew/ci-tune-postgres-wait'", id="t_f21fd58f-couldnt-set-ref"),
    pytest.param("sqlite3.OperationalError: database or disk is full", id="sqlite-full"),
    pytest.param(OSError(errno.ENOSPC, "No space left on device"), id="oserror-enospc"),
]
NOT_DISK_FULL = [
    pytest.param(  # t_23f6977e: a branch already checked out elsewhere
        "workspace: git worktree add failed for /Users/jack/Code/Cybership/assistant/.worktrees/linkedin-render-cap"
        " on branch crew/linkedin-render-cap: Preparing worktree (checking out 'crew/linkedin-render-cap')\nfatal: "
        "'crew/linkedin-render-cap' is already used by worktree at '/Users/jack/Code/Cybership/assistant/.worktrees/"
        "linkedin-render'", id="t_23f6977e-branch-in-use"),
    pytest.param(  # ref races: a bare "cannot lock ref" is not a full disk
        _WT.format(b="x") + "fatal: cannot lock ref 'refs/heads/crew/x': reference already exists",
        id="lock-ref-exists"),
    pytest.param(_WT.format(b="x") + "fatal: cannot lock ref 'refs/heads/crew/x': is at 1a2b3c but expected 4d5e6f",
                 id="lock-ref-moved"),
    pytest.param(  # Hermes's persistence copy: also said for a read-only state.db (guard state covers outages)
        "pid 29179 exited with code 1 [managed worker lost: managed_worker_failed] Worker's last output: \"Session: "
        "local-0801 \u26a0\ufe0f No reply: Hermes couldn't save this conversation to disk, so it stopped rather than "
        "lose your messages. The disk is probably full: free some space (or fix the permissions on "
        "~/.hermes/profiles/crew-pilot-claude-reviewer/state.db), then send your message again.\"",
        id="t_ccae35ed-hermes-couldnt-save"),
    pytest.param(  # t_* "Kanban execution unknown": the owner refused, not the disk
        "pid 23250 exited with code 1 [managed worker lost: managed_worker_failed] Worker's last output: 'Session: "
        "local-4f76 Kanban owner refused: Kanban execution unknown; retained by owner'", id="owner-refused"),
    pytest.param("error: unable to create file a.ts: Permission denied", id="create-file-outside-worktree-add"),
    pytest.param("fatal: couldn't write '/r/.git/refs/heads/x.lock'", id="couldnt-write-outside-worktree-add"),
    pytest.param("sqlite3.OperationalError: disk I/O error", id="sqlite-ioerr"),
    pytest.param(OSError(errno.EACCES, "Permission denied"), id="oserror-eacces"),
    pytest.param("boom", id="other"),
    pytest.param(None, id="none"),
]


@pytest.mark.parametrize("value", INCIDENT_DISK_FULL)
def test_is_disk_full_error_matches_incident_errors(value):
    assert kbdg.is_disk_full_error(value) is True


@pytest.mark.parametrize("value", NOT_DISK_FULL)
def test_is_disk_full_error_rejects_other_failures(value):
    assert kbdg.is_disk_full_error(value) is False


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


@pytest.mark.parametrize("git_stderr", [
    "error: unable to create file x",
    # git's ref-write wordings once the disk is too full to say why (t_3615c4c7, t_f21fd58f)
    "fatal: cannot update ref 'refs/heads/crew/b': couldn't write '/r/.git/refs/heads/crew/b.lock'",
    "fatal: couldn't set 'refs/heads/crew/b'",
])
def test_enospc_workspace_failure_never_counts_or_gives_up(kanban_home, disk, monkeypatch, git_stderr):
    def full_tree(task, board=None):
        raise RuntimeError(f"git worktree add failed for /wt/{task.id} on branch b: Preparing worktree "
                           f"(new branch 'crew/b')\n{git_stderr}")
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
    # Hermes's own refusal is not classified as a full disk (also said for a read-only state.db),
    # but a worker that dies with it during an outage is infrastructure by the guard state.
    (10, "No reply: Hermes couldn't save this conversation to disk, so it stopped rather than lose your "
         "messages. The disk is probably full\n"),
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


def test_recovery_unblocks_a_card_parked_on_a_git_ref_write(kanban_home, disk, monkeypatch):
    """t_f21fd58f's shape: the first spawn said "No space left on device", the second (the one the
    breaker parked it for) only "couldn't set 'refs/heads/...'"."""
    attempts = iter([
        "fatal: cannot lock ref 'refs/heads/crew/b': Unable to create '/r/.git/refs/heads/crew/b.lock': "
        "No space left on device",
        "fatal: couldn't set 'refs/heads/crew/b'",
    ])

    def full_tree(task, board=None):
        raise RuntimeError(f"git worktree add failed for /wt/{task.id} on branch crew/b: Preparing worktree "
                           f"(new branch 'crew/b')\n{next(attempts)}")
    monkeypatch.setattr(kbd._kbw, "_resolve_worktree_workspace", full_tree)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="tree", assignee=REVIEWER, workspace_kind="worktree")
        _fail_spawn(lambda *a, **k: None, conn, tid, 2, guard=None, disk=disk)  # the pre-guard breaker
        assert _task(conn, tid)[0] == "blocked"
        assert "couldn't set" in _events(conn, tid, "gave_up")[-1]["error"]
        assert kbdg.recover_disk_gave_up(conn) == [tid]
        assert tuple(_task(conn, tid)) == ("ready", 0)


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
