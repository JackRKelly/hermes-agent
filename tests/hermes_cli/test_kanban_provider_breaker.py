"""Provider health breaker: a quota wall (exit 75) or lost login (exit 78) pauses every profile on
the same ``model.provider`` instead of respawning each card into the dead provider.

Regression for the Nov 13 incident: 36 runs across 5 profiles on one Claude subscription exited 75
and every card kept respawning; seven exit-78 runs were each parked although the cause was
provider-wide.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_provider_breaker as kbpb

CLAUDE = "claude-sub"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    monkeypatch.setattr(kbd, "_system_memory_sample", dict)
    kbpb._provider_cache.clear()
    for name, provider in (("p1", CLAUDE), ("p2", CLAUDE), ("p3", "openai")):
        prof = home / "profiles" / name
        prof.mkdir(parents=True)
        (prof / "config.yaml").write_text(f"model:\n  provider: {provider}\n")
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn, home


class _Spawner:
    def __init__(self):
        self.pid = 81000

    def __call__(self, task, workspace):
        self.pid += 1
        return self.pid


def _run_and_exit(conn, tid, code, *, host_pid):
    """Claim ``tid`` on this host with a dead worker pid that exited ``code``."""
    host = kb._claimer_id().split(":", 1)[0]
    assert kb.claim_task(conn, tid, claimer=f"{host}:w{host_pid}") is not None
    conn.execute("UPDATE tasks SET worker_pid = ?, worker_started_at = NULL WHERE id = ?", (host_pid, tid))
    conn.commit()
    kbd._record_worker_exit(host_pid, code << 8)


def _breaker(conn, provider=CLAUDE):
    return kbpb._load(conn, provider)


def test_provider_resolves_from_profile_config_else_profile_name(board):
    assert kbpb.provider_for_profile("p1") == kbpb.provider_for_profile("p2") == CLAUDE
    assert kbpb.provider_for_profile("p3") == "openai"
    assert kbpb.provider_for_profile("no-such-profile") == "no-such-profile"


def test_three_quota_exits_trip_breaker_pause_provider_and_spare_other_provider(board):
    conn, _ = board
    cards = [kb.create_task(conn, title=f"c{i}", assignee="p1" if i % 2 else "p2") for i in range(3)]
    for i, tid in enumerate(cards):
        _run_and_exit(conn, tid, kb.KANBAN_RATE_LIMIT_EXIT_CODE, host_pid=82000 + i)
    fourth = kb.create_task(conn, title="c4", assignee="p2")
    other = kb.create_task(conn, title="o1", assignee="p3")

    res = kbd.dispatch_once(conn, spawn_fn=_Spawner())

    rec = _breaker(conn)
    assert rec["state"] == "open" and rec["reason"] == "quota" and rec["trip_id"]
    assert set(rec["profiles"]) == {"p1", "p2"}
    paused = {tid for tid, reason in res.respawn_guarded if reason == "provider_paused"}
    assert fourth in paused and set(cards) <= paused
    assert [s[0] for s in res.spawned] == [other]
    assert "provider_paused=" in kbd.describe_suppression([res])
    # Requeued, never counted as failures.
    assert all(kb.get_task(conn, t).consecutive_failures == 0 for t in cards)


def test_two_quota_exits_do_not_trip(board):
    conn, _ = board
    for i in range(2):
        kbpb.record_exit(conn, "p1", "rate_limited", text="429")
    assert _breaker(conn)["state"] == "closed"
    assert not kbpb.paused(conn, "p1")


def test_half_open_allows_exactly_one_probe_per_interval_and_success_closes(board):
    conn, _ = board
    t0 = int(time.time()) - 200
    for _ in range(3):
        kbpb.record_exit(conn, "p1", "rate_limited", text="429 too many requests", now=t0)
    trip = _breaker(conn)["trip_id"]
    assert kbpb.paused(conn, "p2", now=t0 + 10)
    kbpb.refresh(conn, now=t0 + 10)
    assert _breaker(conn)["state"] == "open"

    kbpb.refresh(conn, now=t0 + 90)
    assert _breaker(conn)["state"] == "half_open"
    probe = kb.create_task(conn, title="probe", assignee="p1")
    assert kbpb.take_probe(conn, "p1", probe, now=t0 + 90)
    assert not kbpb.take_probe(conn, "p2", "t_other", now=t0 + 91)  # one probe per interval
    assert kbpb.paused(conn, "p2", now=t0 + 91)

    claimed = kb.claim_task(conn, probe)
    assert kb.complete_task(conn, probe, summary="ok", expected_run_id=claimed.current_run_id)
    kbpb.refresh(conn)
    rec = _breaker(conn)
    assert rec["state"] == "closed" and rec["trip_id"] == trip
    assert not kbpb.paused(conn, "p1")


def test_probe_quota_exit_reopens_with_same_trip_id(board):
    conn, _ = board
    t0 = int(time.time()) - 200
    for _ in range(3):
        kbpb.record_exit(conn, "p1", "rate_limited", now=t0)
    trip = _breaker(conn)["trip_id"]
    kbpb.refresh(conn, now=t0 + 90)
    assert kbpb.take_probe(conn, "p1", "t_probe", now=t0 + 90)
    assert kbpb.record_exit(conn, "p1", "rate_limited", text="429 again", now=t0 + 95) == "open"
    rec = _breaker(conn)
    assert rec["trip_id"] == trip and rec["probe_task_id"] is None
    assert rec["next_probe_at"] == t0 + 95 + kbpb.DEFAULT_PROBE_SECONDS


def test_known_reset_holds_probes_until_credentials_change(board):
    conn, home = board
    now = int(time.time())
    for _ in range(3):
        kbpb.record_exit(conn, "p1", "rate_limited", text="You've hit your weekly limit, resets in 2 days. "
                         f"reset_at={now + 2 * 86400}", now=now)
    rec = _breaker(conn)
    assert rec["reset_at"] == now + 2 * 86400 and rec["next_probe_at"] == rec["reset_at"]
    kbpb.refresh(conn, now=now + 600)
    assert _breaker(conn)["state"] == "open"  # probe time passed, reset_at did not

    (home / "profiles" / "p1" / "auth.json").write_text("{}")  # an account was added
    kbpb.refresh(conn, now=now + 601)
    rec = _breaker(conn)
    assert rec["state"] == "half_open" and rec["next_probe_at"] == now + 601
    assert kbpb.take_probe(conn, "p1", "t_probe", now=now + 601)


def test_exit_78_while_auth_breaker_open_requeues_instead_of_parking(board):
    conn, _ = board
    for _ in range(3):
        kbpb.record_exit(conn, "p2", "terminal_provider", text="no usable login")
    assert _breaker(conn)["reason"] == "auth"
    tid = kb.create_task(conn, title="t", assignee="p1")
    _run_and_exit(conn, tid, kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE, host_pid=83001)

    crashed = kbd.detect_crashed_workers(conn)

    assert tid not in crashed
    assert tid in kbd.detect_crashed_workers._last_rate_limited
    task = kb.get_task(conn, tid)
    assert task.status == "ready" and task.consecutive_failures == 0


def test_three_simultaneous_login_failures_park_no_card(board):
    conn, _ = board
    cards = [kb.create_task(conn, title=f"c{i}", assignee="p1") for i in range(3)]
    for i, tid in enumerate(cards):
        _run_and_exit(conn, tid, kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE, host_pid=84000 + i)

    kbd.detect_crashed_workers(conn)

    assert _breaker(conn)["state"] == "open"
    assert [kb.get_task(conn, t).status for t in cards] == ["ready"] * 3


def test_lone_exit_78_still_parks(board):
    conn, _ = board
    tid = kb.create_task(conn, title="t", assignee="p1")
    _run_and_exit(conn, tid, kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE, host_pid=85001)

    assert tid in kbd.detect_crashed_workers(conn)

    assert kb.get_task(conn, tid).status == "blocked"
    assert _breaker(conn)["state"] == "closed"


def test_breaker_survives_reconnect(board):
    conn, _ = board
    for _ in range(3):
        kbpb.record_exit(conn, "p1", "rate_limited")
    with kbc.connect_closing() as fresh:
        assert kbpb.paused(fresh, "p2")


@pytest.mark.parametrize("text,expected_delta", [
    ("Retry-After: 120", 120),
    ('{"error": {"retry_after": 3600}}', 3600),
    ("nothing here", None),
])
def test_parse_reset_at(text, expected_delta):
    now = 1_794_600_000
    got = kbpb.parse_reset_at(text, now=now)
    assert (got - now if got is not None else None) == expected_delta


def test_parse_reset_at_month_day_is_in_the_future():
    now = int(time.mktime((2026, 11, 13, 10, 0, 0, 0, 0, -1)))
    got = kbpb.parse_reset_at("429 weekly limit, resets Nov 17", now=now)
    assert time.localtime(got)[:3] == (2026, 11, 17)


def test_message_is_redacted_and_bounded():
    msg = kbpb.redact_message("bad key sk-ant-api03-" + "A" * 80 + " " + "x" * 400)
    assert len(msg) <= kbpb.MESSAGE_MAX_CHARS and "A" * 40 not in msg
