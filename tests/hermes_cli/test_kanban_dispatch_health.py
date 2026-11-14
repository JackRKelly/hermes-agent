"""Dispatch health snapshot (schema 1): the JSON contract the assistant autopilot consumes, written
atomically by every dispatcher tick and printed by ``hermes kanban health --json``. Also: a
``scheduled`` card (CI-wait park) frees its ``max_in_progress`` slot, and a kanban worker never
waits out a long quota ``Retry-After`` inside its slot.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_health as kbh
from hermes_cli import kanban_db_provider_breaker as kbpb

TOP = {"schema", "generated_at", "providers", "dispatch", "idle", "failure_rate"}
PROVIDER = {"provider", "state", "reason", "message", "reset_at", "opened_at", "trip_id", "failures_window",
            "profiles", "next_probe_at"}
DISPATCH = {"ready_spawnable", "bad_ticks", "last_spawn_at", "held_back"}
HELD = {"cap", "per_profile_cap", "active_pr", "provider_paused", "rate_limit_cooldown", "blocker_auth", "other"}
FAILURE = {"profile", "window_seconds", "runs", "failed", "rate_limited", "auth"}


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_system_memory_sample", dict)
    kbpb._provider_cache.clear()
    for name, provider in (("p1", "claude-sub"), ("p3", "openai")):
        prof = home / "profiles" / name
        prof.mkdir(parents=True)
        (prof / "config.yaml").write_text(f"model:\n  provider: {provider}\n")
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def _spawn(task, workspace):
    return None


def _assert_contract(doc):
    assert set(doc) == TOP and doc["schema"] == 1 and isinstance(doc["generated_at"], int)
    for p in doc["providers"]:
        assert set(p) == PROVIDER
        assert p["state"] in {"open", "half_open", "closed"} and p["reason"] in {"quota", "auth"}
        assert len(p["message"]) <= 200 and isinstance(p["profiles"], list)
    assert set(doc["dispatch"]) == DISPATCH and set(doc["dispatch"]["held_back"]) == HELD
    for row in doc["idle"]:
        assert set(row) == {"task_id", "run_id", "profile", "idle_seconds", "state", "at"}
    for row in doc["failure_rate"]:
        assert set(row) == FAILURE and row["window_seconds"] == 3600


def test_tick_writes_contract_snapshot_and_counts_provider_paused(board):
    conn = board
    for _ in range(3):
        kbpb.record_exit(conn, "p1", "rate_limited", text="429 weekly limit, resets Nov 17 token=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx")
    held = kb.create_task(conn, title="held", assignee="p1")

    kbd.dispatch_once(conn, spawn_fn=_spawn)
    path = kbh.snapshot_path(kb.kanban_db_path())
    doc = json.loads(path.read_text())

    _assert_contract(doc)
    [prov] = doc["providers"]
    assert prov["provider"] == "claude-sub" and prov["state"] == "open" and prov["profiles"] == ["p1"]
    assert prov["reset_at"] and prov["failures_window"] == 3 and prov["trip_id"]
    assert "sk-ant-xxxxxxxx" not in prov["message"]
    assert doc["dispatch"]["held_back"]["provider_paused"] == 1
    assert doc["dispatch"]["ready_spawnable"] == 1 and doc["dispatch"]["bad_ticks"] == 1

    kbd.dispatch_once(conn, spawn_fn=_spawn)
    assert json.loads(path.read_text())["dispatch"]["bad_ticks"] == 2
    # trip_id is stable across ticks of one trip
    assert json.loads(path.read_text())["providers"][0]["trip_id"] == prov["trip_id"]

    other = kb.create_task(conn, title="spawns", assignee="p3")
    kbd.dispatch_once(conn, spawn_fn=_spawn)
    doc = json.loads(path.read_text())
    assert doc["dispatch"]["bad_ticks"] == 0 and doc["dispatch"]["last_spawn_at"]
    assert kb.get_task(conn, other).status == "running" and kb.get_task(conn, held).status == "ready"


def test_closed_breaker_listed_for_an_hour_then_dropped(board):
    conn = board
    for _ in range(3):
        kbpb.record_exit(conn, "p1", "rate_limited")
    rec = kbpb._load(conn, "claude-sub")
    now = int(time.time())
    kbpb._close(rec, now)
    with kb.write_txn(conn):
        kbpb._save(conn, rec, now)
    assert [p["state"] for p in kbh.providers_section(conn, now + 60)] == ["closed"]
    assert kbh.providers_section(conn, now + 3601) == []


def test_cap_held_count(board):
    conn = board
    for i in range(3):
        kb.create_task(conn, title=f"t{i}", assignee="p3")
    res = kbd.dispatch_once(conn, spawn_fn=_spawn, max_in_progress=1)
    assert len(res.spawned) == 1 and res.held_cap == 2
    doc = json.loads(kbh.snapshot_path(kb.kanban_db_path()).read_text())
    assert doc["dispatch"]["held_back"]["cap"] == 2


def test_health_cli_prints_contract(board, capsys):
    conn = board
    kb.create_task(conn, title="t", assignee="p3")
    from hermes_cli.kanban import _cmd_health
    assert _cmd_health(argparse.Namespace(json=True)) == 0
    _assert_contract(json.loads(capsys.readouterr().out))
    assert _cmd_health(argparse.Namespace(json=False)) == 0
    assert "ready spawnable" in capsys.readouterr().out


def test_failure_rate_counts_outcomes(board, monkeypatch):
    conn = board
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    host = kb._claimer_id().split(":", 1)[0]
    for i, code in enumerate((kb.KANBAN_RATE_LIMIT_EXIT_CODE, kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE)):
        tid = kb.create_task(conn, title=f"t{i}", assignee="p3")
        kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (86000 + i, tid))
        conn.commit()
        kbd._record_worker_exit(86000 + i, code << 8)
    kbd.detect_crashed_workers(conn)
    [row] = kbh.failure_rate_section(conn, int(time.time()))
    assert row["profile"] == "p3" and row["runs"] == 2
    assert row["rate_limited"] == 1 and row["auth"] == 1 and row["failed"] == 1


def test_scheduled_card_frees_its_slot(board):
    conn = board
    parked = kb.create_task(conn, title="waits on CI", assignee="p3")
    claimed = kb.claim_task(conn, parked)
    assert kb.schedule_task(conn, parked, reason="CI", expected_run_id=claimed.current_run_id)
    assert kb.get_task(conn, parked).status == "scheduled"
    nxt = kb.create_task(conn, title="next", assignee="p3")
    res = kbd.dispatch_once(conn, spawn_fn=_spawn, max_in_progress=1)
    assert [s[0] for s in res.spawned] == [nxt]


class _RateLimited(Exception):
    def __init__(self, retry_after):
        super().__init__("429")
        self.response = SimpleNamespace(headers={"retry-after": str(retry_after)})


@pytest.mark.parametrize("retry_after,worker,ends", [
    (3600, True, True), (61, True, True), (60, True, False), (30, True, False), (3600, False, False),
])
def test_kanban_worker_quota_wait_over_60s_ends_retries(monkeypatch, retry_after, worker, ends):
    from agent.kanban_turn_recovery import quota_wait_ends_turn
    if worker:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    else:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert quota_wait_ends_turn(_RateLimited(retry_after)) is ends


def test_kanban_worker_far_reset_at_ends_retries(monkeypatch):
    from agent.kanban_turn_recovery import quota_wait_ends_turn
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    assert quota_wait_ends_turn(Exception("429 weekly limit"), {"reset_at": time.time() + 4 * 86400})
    assert not quota_wait_ends_turn(Exception("429"), {"reset_at": time.time() + 10})
