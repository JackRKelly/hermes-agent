"""The embedded dispatcher reads ``kanban.disk_guard`` from the user's YAML (no DEFAULT_CONFIG merge):
an absent section is guarded with the defaults, ``enabled: false`` turns it off, and the settings
reach ``dispatch_once`` as its ``disk_guard`` kwarg."""

from __future__ import annotations

import dataclasses

from gateway import kanban_watchers_dispatcher as kwd
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_disk_guard as kbdg


def test_absent_section_is_guarded_with_defaults():
    settings = kwd._resolve_dispatcher_settings({}, kb)
    assert settings.disk_guard == kbdg.DiskGuardSettings()
    assert "disk_guard" not in kwd._NOT_DISPATCH_KWARGS


def test_disabled_section_turns_the_guard_off():
    assert kwd._resolve_dispatcher_settings({"disk_guard": {"enabled": False}}, kb).disk_guard is None


def test_configured_lines_are_read():
    settings = kwd._resolve_dispatcher_settings(
        {"disk_guard": {"heavy_min_free_gb": 80, "all_min_free_gb": 30, "heavy_assignees": ["b-*"]}}, kb)
    guard = settings.disk_guard
    assert guard is not None
    assert dataclasses.astuple(guard)[:2] == (80.0, 30.0)
    assert guard.heavy_assignees == ("b-*",)
