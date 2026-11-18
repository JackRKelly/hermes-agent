"""``kanban.fast_lane``: reply-draft cards spawn past the dispatcher caps.

Regression for the Nov 18 reply-draft delay: an email monitor's draft card sat in ``ready`` for
an hour behind ~100 build cards because ``max_in_progress`` and ``max_in_progress_per_profile``
were full of builds holding slots for hours, and builds already use the top of the priority range.
The fast lane picks cards by ``created_by``, spawns them before the caps are checked, and is
bounded by ``max_running`` plus the memory, breaker and respawn guards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_health as kbh
from hermes_cli import kanban_db_provider_breaker as kbpb
from hermes_cli.kanban_db_dispatch_caps import FastLaneSettings, parse_fast_lane

GIB = 1024 * 1024  # KiB per GiB
DRAFTS = FastLaneSettings(created_by=("email-monitor",), max_running=2)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The test process is the email monitor, outside the worker fleet, even when the suite itself
    # runs inside a dispatched worker.
    for key in ("HERMES_KANBAN_TASK", DELEGATED_CHILD_ENV_MARKER):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    return home


@pytest.fixture
def spawns():
    return []


@pytest.fixture
def spawn_fn(spawns):
    def fake_spawn(task, workspace, board=None):
        # Reports no pid, so the reclaim phase has no worker to probe between ticks.
        spawns.append(task.id)
    return fake_spawn


def _running_builds(conn, n: int, assignee: str = "builder") -> list[str]:
    """``n`` high-priority build cards already holding slots."""
    ids = []
    for i in range(n):
        tid = kb.create_task(conn, title=f"build {i}", assignee=assignee, priority=250, created_by="engineering")
        assert kb.claim_task(conn, tid) is not None
        ids.append(tid)
    return ids


def _draft(conn, title: str = "reply draft", assignee: str = "drafter") -> str:
    return kb.create_task(conn, title=title, assignee=assignee, priority=0, created_by="email-monitor")


def _status(conn, task_id: str) -> str:
    return kb.get_task(conn, task_id).status


def _pressure(monkeypatch, level: str) -> None:
    available = {"critical": 32 * 1024, "elevated": 100 * 1024}[level]
    monkeypatch.setattr(kbd, "_system_memory_sample",
                        lambda: {"mem_available_kib": available, "mem_total_kib": GIB})


def test_draft_spawns_when_global_cap_is_full_and_regular_card_waits(kanban_home, spawns, spawn_fn):
    with kbc.connect() as conn:
        _running_builds(conn, 3)
        draft = _draft(conn)
        build = kb.create_task(conn, title="next build", assignee="builder", priority=300)
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, max_in_progress=3, max_spawn=3, fast_lane=DRAFTS)

        assert spawns == [draft]
        assert res.fast_lane_spawned == [draft]
        assert [s[0] for s in res.spawned] == [draft]
        assert _status(conn, draft) == "running"
        assert _status(conn, build) == "ready"
        assert res.held_cap == 1


def test_draft_spawns_past_its_profiles_cap_and_counts_against_it(kanban_home, spawns, spawn_fn):
    with kbc.connect() as conn:
        _running_builds(conn, 1, assignee="drafter")
        draft = _draft(conn)
        other = kb.create_task(conn, title="drafter chore", assignee="drafter", priority=100)
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, max_in_progress_per_profile=1, fast_lane=DRAFTS)

    assert spawns == [draft]
    # The fast-lane run counts as the profile's: 1 build + 1 draft running.
    assert res.skipped_per_profile_capped == [(other, "drafter", 2)]


def test_max_running_holds_the_next_draft_until_one_finishes(kanban_home, spawns, spawn_fn):
    lane = FastLaneSettings(created_by=("email-monitor",), max_running=1)
    with kbc.connect() as conn:
        _running_builds(conn, 2)
        first = _draft(conn, "draft 1")
        second = _draft(conn, "draft 2")
        def tick() -> list[str]:
            return kbd.dispatch_once(conn, spawn_fn=spawn_fn, max_in_progress=2, fast_lane=lane).fast_lane_spawned

        assert tick() == [first]
        assert tick() == []
        assert _status(conn, second) == "ready"

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done', claim_lock = NULL WHERE id = ?", (first,))
        assert tick() == [second]
    assert spawns == [first, second]


@pytest.mark.parametrize("raw", [None, {}, {"created_by": []}, {"created_by": ["", "  "]}, "email-monitor"])
def test_absent_or_empty_fast_lane_keeps_todays_caps(kanban_home, spawns, spawn_fn, raw):
    assert parse_fast_lane(raw) is None
    with kbc.connect() as conn:
        _running_builds(conn, 3)
        draft = _draft(conn)
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, max_in_progress=3, fast_lane=parse_fast_lane(raw))
        assert _status(conn, draft) == "ready"
    assert spawns == []
    assert res.fast_lane_spawned == []
    assert res.held_cap == 1


def test_parse_fast_lane_reads_names_and_max_running():
    assert parse_fast_lane({"created_by": ["email-monitor", "email-monitor", " pager "], "max_running": 3}) == \
        FastLaneSettings(created_by=("email-monitor", "pager"), max_running=3)
    assert parse_fast_lane({"created_by": "a, b"}) == FastLaneSettings(created_by=("a", "b"), max_running=2)
    assert parse_fast_lane({"created_by": ["a"], "max_running": 0}).max_running == 2


def test_critical_memory_pressure_spawns_no_draft(kanban_home, spawns, spawn_fn, monkeypatch):
    _pressure(monkeypatch, "critical")
    with kbc.connect() as conn:
        draft = _draft(conn)
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, fast_lane=DRAFTS)
        assert _status(conn, draft) == "ready"
    assert spawns == []
    assert res.memory_pressure == "critical"


def test_elevated_memory_pressure_allows_one_new_worker_in_total(kanban_home, spawns, spawn_fn, monkeypatch):
    _pressure(monkeypatch, "elevated")
    with kbc.connect() as conn:
        first = _draft(conn, "draft 1")
        _draft(conn, "draft 2")
        kb.create_task(conn, title="build", assignee="builder", priority=300)
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, fast_lane=DRAFTS)
    assert spawns == [first]
    assert res.fast_lane_spawned == [first]
    assert res.memory_pressure == "elevated"


def test_respawn_guarded_draft_is_not_spawned_and_recorded_once(kanban_home, spawns, spawn_fn, monkeypatch):
    real_guard = kbd.check_respawn_guard
    with kbc.connect() as conn:
        draft = _draft(conn)
        monkeypatch.setattr(kbd, "check_respawn_guard",
                            lambda c, tid, lane="ready": "active_pr" if tid == draft else real_guard(c, tid, lane=lane))
        # Room under the caps: the normal loop must not try the same row a second time.
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, max_in_progress=5, fast_lane=DRAFTS)
        assert _status(conn, draft) == "ready"
        guarded_events = [e for e in kb.list_events(conn, draft) if e.kind == "respawn_guarded"]
    assert spawns == []
    assert res.respawn_guarded == [(draft, "active_pr")]
    assert len(guarded_events) == 1


def test_provider_paused_draft_is_not_spawned(kanban_home, spawns, spawn_fn, monkeypatch):
    real_paused = kbpb.paused
    monkeypatch.setattr(kbpb, "paused", lambda c, profile, now=None, *, note=False:
                        profile == "drafter" or real_paused(c, profile, now, note=note))
    with kbc.connect() as conn:
        _running_builds(conn, 3)
        draft = _draft(conn)
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, max_in_progress=3, fast_lane=DRAFTS)
        assert _status(conn, draft) == "ready"
    assert spawns == []
    assert res.respawn_guarded == [(draft, "provider_paused")]


def test_dry_run_counts_fast_lane_spawns_against_the_budget(kanban_home):
    with kbc.connect() as conn:
        _running_builds(conn, 1)
        draft = _draft(conn)
        kb.create_task(conn, title="build a", assignee="builder", priority=300)
        kb.create_task(conn, title="build b", assignee="builder", priority=300)
        res = kbd.dispatch_once(conn, dry_run=True, max_in_progress=3, fast_lane=DRAFTS)
    # 3 slots - 1 running build - 1 draft = 1 build, the same as a real tick would spawn.
    assert res.fast_lane_spawned == [draft]
    assert len(res.spawned) == 2


def _cli_draft(monkeypatch, capsys, env: dict[str, str]) -> str:
    """A ``created_by=email-monitor`` card made the way anyone can: ``hermes kanban create``."""
    import argparse
    import json

    from hermes_cli.kanban import kanban_command
    from hermes_cli.kanban_parser import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    with monkeypatch.context() as m:
        for key, value in env.items():
            m.setenv(key, value)
        argv = ["kanban", "create", "reply draft", "--assignee", "drafter", "--created-by", "email-monitor", "--json"]
        assert kanban_command(parser.parse_args(argv)) == 0
    return json.loads(capsys.readouterr().out)["id"]


def _api_draft(monkeypatch, env: dict[str, str]) -> str:
    """The same card from a script calling the board API directly (no ``creator_task_id``)."""
    with monkeypatch.context() as m:
        for key, value in env.items():
            m.setenv(key, value)
        with kbc.connect() as conn:
            return _draft(conn)


@pytest.mark.parametrize("surface", ["cli", "api"])
def test_creator_forged_inside_a_worker_gets_no_fast_lane(kanban_home, spawns, spawn_fn,
                                                          monkeypatch, capsys, surface):
    with kbc.connect() as conn:
        _running_builds(conn, 3)
        worker = kb.create_task(conn, title="worker", assignee="builder")
    env = {"HERMES_KANBAN_TASK": worker}
    forged = _cli_draft(monkeypatch, capsys, env) if surface == "cli" else _api_draft(monkeypatch, env)
    real = _cli_draft(monkeypatch, capsys, {})
    with kbc.connect() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, max_in_progress=3, fast_lane=DRAFTS)
        assert _status(conn, forged) == "ready"
    assert res.fast_lane_spawned == [real]
    assert spawns == [real]


def test_forged_running_card_does_not_hold_a_fast_lane_slot(kanban_home, spawns, spawn_fn, monkeypatch, capsys):
    lane = FastLaneSettings(created_by=("email-monitor",), max_running=1)
    with kbc.connect() as conn:
        worker = kb.create_task(conn, title="worker", assignee="builder")
    forged = _cli_draft(monkeypatch, capsys, {"HERMES_KANBAN_TASK": worker})
    with kbc.connect() as conn:
        assert kb.claim_task(conn, forged) is not None  # e.g. spawned by the normal loop
        draft = _draft(conn)
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, max_in_progress=2, fast_lane=lane)
    assert res.fast_lane_spawned == [draft]


def test_elevated_pressure_with_full_caps_still_reports_the_cap_hold(kanban_home, spawns, spawn_fn, monkeypatch):
    _pressure(monkeypatch, "elevated")
    with kbc.connect() as conn:
        _running_builds(conn, 3)
        draft = _draft(conn)
        kb.create_task(conn, title="next build", assignee="builder", priority=300)
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, max_in_progress=3, fast_lane=DRAFTS)
    assert res.fast_lane_spawned == [draft]
    # The cap, not memory, holds the build: the same counts as a tick without the lane.
    assert res.memory_pressure is None
    assert res.held_cap == 1


def test_health_snapshot_counts_fast_lane_spawns(kanban_home, spawn_fn):
    with kbc.connect() as conn:
        _running_builds(conn, 3)
        _draft(conn)
        res = kbd.dispatch_once(conn, spawn_fn=spawn_fn, max_in_progress=3, fast_lane=DRAFTS)
        section = kbh.dispatch_section(conn, res, None, now=0)
    assert section["fast_lane_spawned"] == 1
