"""Kanban concurrency caps apply live, without a gateway restart.

The embedded dispatcher used to read ``kanban.max_in_progress`` and
``kanban.max_in_progress_per_profile`` once at boot, so every concurrency change needed a
gateway restart. These tests drive the real dispatcher object against a real config.yaml and
kanban DB in a temp ``HERMES_HOME``: an edit applies on the next tick, an invalid edit is ignored
with one warning, and lowering a cap never reclaims a running worker.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pytest

from gateway import kanban_watchers_dispatcher as kwd
from hermes_cli import kanban_caps
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.config import get_config_path, load_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Temp HERMES_HOME with an empty board and two spawnable profiles."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("alpha", "beta"):
        prof = hermes_home / "profiles" / name
        prof.mkdir(parents=True)
        (prof / "config.yaml").write_text("{}\n", encoding="utf-8")
    kb.init_db()
    return hermes_home


def _write_caps(text: str) -> None:
    get_config_path().write_text(f"kanban:\n{text}", encoding="utf-8")


def _boot_dispatcher() -> kwd._KanbanDispatcher:
    """Build the dispatcher exactly as the gateway watcher does at boot."""
    kanban_cfg = load_config().get("kanban", {})
    settings = kwd._resolve_dispatcher_settings(kanban_cfg, kb)
    return kwd._KanbanDispatcher(kb, settings, boot_kanban_cfg=kanban_cfg, config_path=get_config_path())


@pytest.fixture
def spawn_capture(monkeypatch):
    """Run the real ``dispatch_once`` with a fake spawner (this test process's pid, so crash
    detection sees a live worker) and record the caps every tick received."""
    real = kbd.dispatch_once
    ticks: list[dict] = []

    def fake_spawn(task, workspace, board=None):
        return os.getpid()

    def capturing(conn, **kwargs):
        ticks.append(dict(kwargs))
        return real(conn, spawn_fn=fake_spawn, **kwargs)

    monkeypatch.setattr(kbd, "dispatch_once", capturing)
    return ticks


def _statuses() -> dict[str, int]:
    with kbc.connect_closing() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
    return {row["status"]: int(row["n"]) for row in rows}


def _tick(dispatcher: kwd._KanbanDispatcher) -> Any:
    """One loop iteration as the gateway runs it: refresh caps from config, then tick."""
    dispatcher.refresh_caps(kanban_caps.read_kanban_section())
    return dispatcher.tick_once_for_board(kb.DEFAULT_BOARD)


def test_config_edit_mid_run_applies_on_next_tick(home, spawn_capture, caplog):
    _write_caps("  max_in_progress: 10\n  max_in_progress_per_profile: 5\n")
    dispatcher = _boot_dispatcher()
    _tick(dispatcher)
    assert (spawn_capture[-1]["max_in_progress"], spawn_capture[-1]["max_in_progress_per_profile"]) == (10, 5)

    # The CLI writer path: validated, backed up, atomic.
    caps, backup = kanban_caps.save_caps(total=14, per_profile=7, set_profiles={"alpha": 7})
    assert caps.label() == "14/7 [alpha=7]"
    assert backup is not None and backup.is_file()

    with caplog.at_level(logging.INFO):
        _tick(dispatcher)
    assert spawn_capture[-1]["max_in_progress"] == 14
    assert spawn_capture[-1]["max_in_progress_per_profile"] == 7
    assert spawn_capture[-1]["max_in_progress_by_profile"] == {"alpha": 7}
    assert "caps changed 10/5 -> 14/7 [alpha=7] (applied live, no restart)" in caplog.text
    # The live record now matches config.yaml, so the CLI/dashboard report "in sync".
    assert kanban_caps.caps_summary()["in_sync"] is True


def test_invalid_edit_is_ignored_with_one_warning(home, spawn_capture, caplog):
    _write_caps("  max_in_progress: 10\n  max_in_progress_per_profile: 5\n")
    dispatcher = _boot_dispatcher()
    _tick(dispatcher)

    _write_caps("  max_in_progress: 14\n  max_in_progress_per_profile: 20\n")
    with caplog.at_level(logging.INFO):
        _tick(dispatcher)
        _tick(dispatcher)
    assert (spawn_capture[-1]["max_in_progress"], spawn_capture[-1]["max_in_progress_per_profile"]) == (10, 5)
    warnings = [r for r in caplog.records if "ignoring invalid caps" in r.getMessage()]
    assert len(warnings) == 1, "an unchanged invalid file must not warn every tick"
    assert "max_in_progress_per_profile=20 is above max_in_progress=14" in warnings[0].getMessage()
    summary = kanban_caps.caps_summary()
    assert summary["in_sync"] is False and summary["config_errors"]


def test_lowering_a_cap_holds_new_spawns_and_never_reclaims(home, spawn_capture):
    with kbc.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f"a{i}", assignee="alpha") for i in range(6)]
    _write_caps("  max_in_progress: 4\n")
    dispatcher = _boot_dispatcher()
    _tick(dispatcher)
    assert _statuses() == {"running": 4, "ready": 2}

    _write_caps("  max_in_progress: 2\n")
    result = _tick(dispatcher)
    assert spawn_capture[-1]["max_in_progress"] == 2
    assert result.spawned == [] and result.reclaimed == 0
    assert _statuses() == {"running": 4, "ready": 2}, "running workers keep running under a lower cap"

    # Two workers finish: still at the new cap of 2, so nothing new starts yet.
    with kbc.connect_closing() as conn:
        running = [tid for tid in ids if kb.get_task(conn, tid).status == "running"]
        for tid in running[:2]:
            kb.complete_task(conn, tid, summary="done", force=True)
    assert _tick(dispatcher).spawned == []
    # A third finishes: one slot opens under the cap of 2.
    with kbc.connect_closing() as conn:
        kb.complete_task(conn, running[2], summary="done", force=True)
    assert len(_tick(dispatcher).spawned) == 1


def test_per_lane_override_beats_the_per_profile_default(home, spawn_capture):
    with kbc.connect_closing() as conn:
        for i in range(4):
            kb.create_task(conn, title=f"a{i}", assignee="alpha")
            kb.create_task(conn, title=f"b{i}", assignee="beta")
    _write_caps("  max_in_progress: 8\n  max_in_progress_per_profile: 1\n"
                "  max_in_progress_by_profile:\n    alpha: 3\n")
    result = _tick(_boot_dispatcher())
    assignees = [who for _tid, who, _ws in result.spawned]
    assert assignees.count("alpha") == 3
    assert assignees.count("beta") == 1


@pytest.mark.parametrize("section, message", [
    ({"max_in_progress": 33}, "outside 1..32"),
    ({"max_in_progress": 0}, "outside 1..32"),
    ({"max_in_progress": "many"}, "not an integer"),
    ({"max_in_progress": True}, "not an integer"),
    ({"max_in_progress": 4, "max_in_progress_by_profile": {"alpha": 5}}, "above max_in_progress=4"),
    ({"max_in_progress_by_profile": ["alpha"]}, "must be a mapping"),
])
def test_parse_caps_rejects_the_whole_set(section, message):
    caps, errors = kanban_caps.parse_caps(section)
    assert caps is None
    assert any(message in err for err in errors)


def test_save_caps_refuses_invalid_and_leaves_the_file_untouched(home):
    _write_caps("  max_in_progress: 10\n")
    before = get_config_path().read_bytes()
    with pytest.raises(ValueError, match="above max_in_progress=10"):
        kanban_caps.save_caps(per_profile=12)
    assert get_config_path().read_bytes() == before


def test_boot_keeps_a_legacy_out_of_range_total(home, caplog):
    """An install already running at 40 keeps 40 after upgrading (only live edits are bounded)."""
    _write_caps("  max_in_progress: 40\n")
    with caplog.at_level(logging.WARNING):
        dispatcher = _boot_dispatcher()
    assert dispatcher.settings.max_in_progress == 40
    assert "fail validation" in caplog.text
