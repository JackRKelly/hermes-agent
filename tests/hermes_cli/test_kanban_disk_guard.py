"""Disk-space dispatch guard (2026-11-19 disk-full incident).

A crew filled the Data volume; every spawn then crashed with ENOSPC and each
retry wrote more. Below ``kanban.disk_critical_gb`` the dispatcher spawns
nothing; below ``kanban.disk_elevated_gb`` at most one worker per tick.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.mark.parametrize(
    "free,level",
    [(None, "unknown"), (5.0, "critical"), (39.9, "critical"), (40.0, "elevated"),
     (119.0, "elevated"), (120.0, "ok"), (800.0, "ok")],
)
def test_disk_pressure_levels_default_thresholds(kanban_home, free, level):
    assert kbd._disk_pressure_level(free) == level


def test_disk_pressure_reads_config_thresholds(kanban_home, monkeypatch):
    import hermes_cli.config as cfg
    monkeypatch.setattr(
        cfg, "load_config_readonly",
        lambda: {"kanban": {"disk_critical_gb": 10, "disk_elevated_gb": 20}},
    )
    assert kbd._disk_pressure_level(15.0) == "elevated"
    assert kbd._disk_pressure_level(25.0) == "ok"
    assert kbd._disk_pressure_level(5.0) == "critical"


def _dispatch(monkeypatch, free_gb, n=3):
    monkeypatch.setattr(kbd, "_disk_free_gb", lambda path=None: free_gb)
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kbc.connect() as conn:
        for i in range(n):
            kb.create_task(conn, title=f"t{i}", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
    return spawns, res


def test_disk_critical_spawns_nothing(kanban_home, all_assignees_spawnable, monkeypatch):
    spawns, res = _dispatch(monkeypatch, 1.0)
    assert not spawns
    assert res.memory_pressure == "disk-critical"


def test_disk_elevated_spawns_at_most_one(kanban_home, all_assignees_spawnable, monkeypatch):
    spawns, res = _dispatch(monkeypatch, 60.0)
    assert len(spawns) <= 1
    assert res.memory_pressure == "disk-elevated"


def test_disk_unknown_imposes_no_restriction(kanban_home, all_assignees_spawnable, monkeypatch):
    spawns, res = _dispatch(monkeypatch, None)
    assert len(spawns) == 3
