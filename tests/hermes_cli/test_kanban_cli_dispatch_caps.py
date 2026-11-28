"""``hermes kanban dispatch`` applies the caps of the config the gateway dispatcher reads.

Regression for the Nov 27, 2026 fan-out: a pass from a profile whose ``config.yaml`` had no
``kanban:`` section read only that file, so the host total (12) and per-profile (9) caps the
gateway dispatcher enforces never applied and the pass spawned 78 workers past them. The CLI
now takes its whole ``kanban:`` section from the config the gateway dispatcher reads — the path
recorded in the live-caps record, else the default home's ``config.yaml``, else the invoking
profile's own file — and reports the caps applied plus what the caps held back.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_caps as kcaps
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def home(tmp_path, monkeypatch, all_assignees_spawnable):
    """A fresh default home: HERMES_HOME points at it and the board DB exists."""
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The test process is the operator's shell, outside the worker fleet, even when the
    # suite itself runs inside a dispatched worker.
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK",
                DELEGATED_CHILD_ENV_MARKER):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    return root


@pytest.fixture
def spawns(monkeypatch):
    """Ids the real ``dispatch_once`` spawned, with worker spawning stubbed out."""
    ids: list[str] = []
    real = kbd.dispatch_once

    def patched(conn, **kwargs):
        # Reports no pid, so the reclaim phase has no worker to probe between ticks.
        kwargs.setdefault("spawn_fn", lambda task, workspace, board=None: ids.append(task.id))
        return real(conn, **kwargs)

    monkeypatch.setattr(kbd, "dispatch_once", patched)
    return ids


def _write_config(root: Path, text: str) -> None:
    (root / "config.yaml").write_text(text, encoding="utf-8")


def _ready_tasks(*specs: tuple[str, int]) -> list[str]:
    """One ready card per ``(assignee, priority)``, highest priority first in dispatch order."""
    with kbc.connect_closing() as conn:
        return [
            kb.create_task(conn, title=f"card {i}", assignee=who, priority=prio)
            for i, (who, prio) in enumerate(specs)
        ]


def _run(*argv: str) -> int:
    top = argparse.ArgumentParser(prog="hermes")
    kanban_cli.build_parser(top.add_subparsers(dest="command"))
    return kanban_cli.kanban_command(top.parse_args(["kanban", *argv]))


def test_total_cap_holds_back_the_rest(home, spawns, capsys):
    _write_config(home, "kanban:\n  max_in_progress: 2\n")
    first, second, third, fourth = _ready_tasks(
        ("builder", 100), ("builder", 90), ("builder", 80), ("builder", 70))
    assert _run("dispatch") == 0
    assert spawns == [first, second]
    out = capsys.readouterr().out
    assert f"Caps: 2 total / no per-profile cap ({home / 'config.yaml'})" in out
    assert ("Held back by caps: 2 (host cap: 2, per-profile caps: 0); "
            "--ignore-caps exceeds them") in out
    assert "Deferred" not in out
    with kbc.connect_closing() as conn:
        unfinished = (kb.get_task(conn, third), kb.get_task(conn, fourth))
    assert [task.status if task is not None else None for task in unfinished] == ["ready", "ready"]


def test_per_profile_cap_defers_the_next_card(home, spawns, capsys):
    _write_config(home, "kanban:\n  max_in_progress: 10\n  max_in_progress_per_profile: 1\n")
    first, second = _ready_tasks(("builder", 100), ("builder", 90))
    assert _run("dispatch") == 0
    assert spawns == [first]
    out = capsys.readouterr().out
    assert f"Caps: 10 total / 1 per profile ({home / 'config.yaml'})" in out
    assert f"Deferred (builder at per-profile cap, 1 running): {second}" in out
    assert "Held back by caps: 1 (host cap: 0, per-profile caps: 1)" in out


def test_by_profile_override_caps_one_lane_only(home, spawns, capsys):
    _write_config(home, (
        "kanban:\n"
        "  max_in_progress: 10\n"
        "  max_in_progress_per_profile: 3\n"
        "  max_in_progress_by_profile:\n"
        "    builder: 1\n"
    ))
    first, second = _ready_tasks(("builder", 100), ("builder", 90))
    helper_one, helper_two = _ready_tasks(("helper", 80), ("helper", 70))
    assert _run("dispatch") == 0
    assert spawns == [first, helper_one, helper_two]
    out = capsys.readouterr().out
    assert "Caps: 10 total / 3 per profile [builder=1] (" in out
    assert f"Deferred (builder at per-profile cap, 1 running): {second}" in out


def test_dispatch_from_another_profile_applies_the_dispatchers_caps(
    home, spawns, capsys, monkeypatch,
):
    """The Nov 27 shape: the invoking profile's config has no kanban section; the caps live
    in the config the gateway dispatcher read (recorded in the live-caps record)."""
    _write_config(home, "kanban:\n  max_in_progress: 2\n")
    other = home / "profiles" / "other"
    other.mkdir(parents=True)
    (other / "config.yaml").write_text("display:\n  skin: default\n", encoding="utf-8")
    kcaps.write_live_caps(kcaps.KanbanCaps(total=2), effective_total=2,
                          config_path=home / "config.yaml")
    first, second, _third = _ready_tasks(("builder", 100), ("builder", 90), ("builder", 80))
    monkeypatch.setenv("HERMES_HOME", str(other))
    assert _run("dispatch") == 0
    assert spawns == [first, second]
    out = capsys.readouterr().out
    assert f"Caps: 2 total / no per-profile cap ({home / 'config.yaml'})" in out


def test_without_a_record_the_default_home_config_wins_over_the_profile(
    home, spawns, capsys, monkeypatch,
):
    other = home / "profiles" / "other"
    other.mkdir(parents=True)
    (other / "config.yaml").write_text("kanban:\n  max_in_progress: 3\n", encoding="utf-8")
    _write_config(home, "kanban:\n  max_in_progress: 2\n")
    first, second, _third = _ready_tasks(("builder", 100), ("builder", 90), ("builder", 80))
    monkeypatch.setenv("HERMES_HOME", str(other))
    assert _run("dispatch") == 0
    assert spawns == [first, second]
    assert f"Caps: 2 total / no per-profile cap ({home / 'config.yaml'})" in capsys.readouterr().out


def test_invalid_caps_fall_back_like_the_gateway_boot(home, spawns, capsys):
    """A half-valid set (per-profile above total) must not drop every cap: the gateway boots
    on the plain integer values, so this pass does too."""
    _write_config(home, "kanban:\n  max_in_progress: 1\n  max_in_progress_per_profile: 5\n")
    first, _second = _ready_tasks(("builder", 100), ("builder", 90))
    assert _run("dispatch") == 0
    assert spawns == [first]
    captured = capsys.readouterr()
    assert "is above max_in_progress=1" in captured.err
    assert f"Caps: 1 total / 5 per profile ({home / 'config.yaml'})" in captured.out


def test_ignore_caps_spawns_past_them(home, spawns, capsys):
    _write_config(home, "kanban:\n  max_in_progress: 1\n  max_in_progress_per_profile: 1\n")
    first, second, third = _ready_tasks(
        ("builder", 100), ("builder", 90), ("helper", 80))
    assert _run("dispatch") == 0
    assert spawns == [first]
    capsys.readouterr()
    assert _run("dispatch", "--ignore-caps") == 0
    assert spawns == [first, second, third]
    captured = capsys.readouterr()
    assert "--ignore-caps" in captured.err
    assert f"Caps: 1 total / 1 per profile ({home / 'config.yaml'})" in captured.out


def test_dry_run_reports_caps_and_holds_without_spawning(home, spawns, capsys):
    _write_config(home, "kanban:\n  max_in_progress: 2\n  max_in_progress_per_profile: 1\n")
    _ready_tasks(("builder", 100), ("builder", 90), ("helper", 80))
    assert _run("dispatch", "--dry-run") == 0
    assert spawns == []
    out = capsys.readouterr().out
    assert f"Caps: 2 total / 1 per profile ({home / 'config.yaml'})" in out
    assert "Held back by caps: 1 (host cap: 0, per-profile caps: 1)" in out


def test_json_carries_caps_and_held_back_counts(home, spawns, capsys):
    _write_config(home, "kanban:\n  max_in_progress: 2\n")
    first, second, _third = _ready_tasks(("builder", 100), ("builder", 90), ("builder", 80))
    assert _run("dispatch", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["caps"] == {
        "total": 2, "per_profile": None, "by_profile": {},
        "effective_total": 2, "source": str(home / "config.yaml"),
    }
    assert payload["held_back"] == {"cap": 1, "per_profile_cap": 0}
    assert payload["caps_ignored"] is False
    assert [s["task_id"] for s in payload["spawned"]] == [first, second]


def test_json_marks_ignored_caps(home, spawns, capsys):
    _write_config(home, "kanban:\n  max_in_progress: 1\n")
    _ready_tasks(("builder", 100), ("builder", 90))
    assert _run("dispatch", "--json", "--ignore-caps") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["caps_ignored"] is True
    assert payload["caps"]["total"] == 1  # what the dispatcher's config says
    assert len(payload["spawned"]) == 2  # what this pass did
    assert payload["held_back"] == {"cap": 0, "per_profile_cap": 0}
