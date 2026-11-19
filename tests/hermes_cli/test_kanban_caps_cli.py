"""``hermes kanban caps`` shows and edits the live concurrency caps.

Driven through the real argparse tree and ``kanban_command`` against a temp ``HERMES_HOME``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import hermes_yaml as yaml

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_caps
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.config import get_config_path


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    get_config_path().write_text("# my notes\nkanban:\n  max_in_progress: 10\n", encoding="utf-8")
    kb.init_db()
    return hermes_home


def _run(*argv: str) -> int:
    top = argparse.ArgumentParser(prog="hermes")
    kanban_cli.build_parser(top.add_subparsers(dest="command"))
    return kanban_cli.kanban_command(top.parse_args(["kanban", *argv]))


def test_set_writes_config_and_keeps_comments(home, capsys):
    assert _run("caps", "set", "--total", "14", "--per-profile", "7", "--profile", "builder=7") == 0
    text = get_config_path().read_text(encoding="utf-8")
    assert "# my notes" in text
    kanban = yaml.safe_load(text)["kanban"]
    assert kanban["max_in_progress"] == 14
    assert kanban["max_in_progress_per_profile"] == 7
    assert kanban["max_in_progress_by_profile"] == {"builder": 7}
    assert "no restart needed" in capsys.readouterr().out

    assert _run("caps", "set", "--clear-profile", "builder") == 0
    assert "max_in_progress_by_profile" not in yaml.safe_load(get_config_path().read_text())["kanban"]


def test_set_rejects_invalid_caps(home, capsys):
    assert _run("caps", "set", "--per-profile", "11") != 0
    assert "above max_in_progress=10" in capsys.readouterr().err
    assert yaml.safe_load(get_config_path().read_text())["kanban"] == {"max_in_progress": 10}


def test_set_zero_is_refused_as_out_of_range_not_as_empty(home, capsys):
    assert _run("caps", "set", "--total", "0") != 0
    err = capsys.readouterr().err
    assert "nothing to set" not in err and "1..32" in err
    assert yaml.safe_load(get_config_path().read_text())["kanban"] == {"max_in_progress": 10}


def test_summary_compares_against_the_dispatchers_config(home, tmp_path):
    """A caller on another profile still sees in_sync against the file the dispatcher reads."""
    dispatcher_cfg = tmp_path / "dispatcher" / "config.yaml"
    dispatcher_cfg.parent.mkdir()
    dispatcher_cfg.write_text("kanban:\n  max_in_progress: 14\n  max_in_progress_per_profile: 7\n", encoding="utf-8")
    kanban_caps.write_live_caps(kanban_caps.KanbanCaps(total=14, per_profile=7), effective_total=14,
                                config_path=dispatcher_cfg)
    summary = kanban_caps.caps_summary()
    assert summary["config_path"] == str(dispatcher_cfg)
    assert summary["configured"]["total"] == 14
    assert summary["in_sync"] is True


def test_set_refuses_a_config_the_dispatcher_does_not_read(home, tmp_path, capsys):
    kanban_caps.write_live_caps(kanban_caps.KanbanCaps(total=10), effective_total=10,
                                config_path=tmp_path / "elsewhere" / "config.yaml")
    assert _run("caps", "set", "--total", "12") != 0
    assert "the live dispatcher reads" in capsys.readouterr().err


def _leave_stale_record(config_path: Path, **overrides) -> None:
    """A record as a gateway that has since stopped would leave it: written by this process,
    then its owner fields changed so they no longer name a running process."""
    kanban_caps.write_live_caps(kanban_caps.KanbanCaps(total=10), effective_total=10, config_path=config_path)
    path = kanban_caps.live_caps_path()
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(overrides)
    path.write_text(json.dumps({k: v for k, v in record.items() if v is not None}), encoding="utf-8")


@pytest.mark.parametrize("owner", [
    # Same pid, different start time: the pid was recycled by an unrelated process.
    {"pid_start_time": -1},
    # A record from before the owner fields existed.
    {"pid": None, "pid_start_time": None},
], ids=["recycled-pid", "no-owner"])
def test_stale_record_is_not_shown_as_live_and_does_not_drive_the_refusal(home, tmp_path, capsys, owner):
    _leave_stale_record(tmp_path / "elsewhere" / "config.yaml", **owner)
    summary = kanban_caps.caps_summary()
    assert summary["live"] is None and summary["live_state"] == "stale"
    assert summary["in_sync"] is False
    assert summary["config_path"] == str(get_config_path())

    assert _run("caps") == 0
    out = capsys.readouterr().out
    assert "no live dispatcher record" in out and "no longer running" in out

    # The old record names another profile's config.yaml; with its dispatcher gone it must not
    # block an edit of this profile's file.
    assert _run("caps", "set", "--total", "12") == 0
    assert yaml.safe_load(get_config_path().read_text())["kanban"]["max_in_progress"] == 12


def test_record_whose_writer_exited_is_stale(home):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()  # reaped: the pid names no process now
    kanban_caps.write_live_caps(kanban_caps.KanbanCaps(total=10), effective_total=10, config_path=get_config_path())
    record = kanban_caps.read_live_caps_record()
    assert record is not None
    assert kanban_caps.live_record_is_current({**record, "pid": child.pid}) is False


def test_record_from_the_running_dispatcher_is_live(home):
    kanban_caps.write_live_caps(kanban_caps.KanbanCaps(total=10), effective_total=10, config_path=get_config_path())
    record = kanban_caps.read_live_caps_record()
    assert record is not None
    assert record["pid"] == os.getpid() and record["pid_start_time"] is not None
    assert kanban_caps.caps_summary()["live_state"] == "live"


def test_show_json_reports_lanes_and_live_state(home, capsys):
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="a", assignee="alpha")
    kanban_caps.write_live_caps(kanban_caps.KanbanCaps(total=10), effective_total=10, config_path=get_config_path())
    assert _run("caps", "--json") == 0
    snap = json.loads(capsys.readouterr().out)
    assert snap["configured"]["total"] == 10
    assert snap["in_sync"] is True
    assert snap["lanes"]["alpha"] == {"running": 0, "ready": 1, "cap": None}


def test_slash_can_read_but_not_set(home):
    assert "configured" in kanban_cli.run_slash("caps")
    assert "terminal-only" in kanban_cli.run_slash("caps set --total 12")
    assert yaml.safe_load(get_config_path().read_text())["kanban"] == {"max_in_progress": 10}
