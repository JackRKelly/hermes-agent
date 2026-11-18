"""The embedded dispatcher reads ``kanban.fast_lane`` and hands it to every board tick."""

from __future__ import annotations

from types import SimpleNamespace

from gateway import kanban_watchers_dispatcher as kwd
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_dispatch_caps import FastLaneSettings


def test_fast_lane_setting_reaches_dispatch_once(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    settings = kwd._resolve_dispatcher_settings(
        {"fast_lane": {"created_by": ["email-monitor"], "max_running": 3}, "max_in_progress_per_profile": 6}, kb)
    assert settings.fast_lane == FastLaneSettings(created_by=("email-monitor",), max_running=3)

    captured = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    from hermes_cli import kanban_db_connect, kanban_db_dispatch
    monkeypatch.setattr(kanban_db_dispatch, "dispatch_once", fake_dispatch_once)
    monkeypatch.setattr(kanban_db_connect, "connect", lambda board=None: SimpleNamespace(close=lambda: None))
    dispatcher = kwd._KanbanDispatcher(kb, settings)
    monkeypatch.setattr(dispatcher, "board_db_fingerprint", lambda slug: ("x", None, None))
    dispatcher.tick_once_for_board("default")

    # The settings object itself, not a dict copy of it.
    assert captured["fast_lane"] is settings.fast_lane
    assert captured["max_in_progress_per_profile"] == 6


def test_fast_lane_absent_is_off(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert kwd._resolve_dispatcher_settings({}, kb).fast_lane is None
    assert kwd._resolve_dispatcher_settings({"fast_lane": {"created_by": []}}, kb).fast_lane is None
