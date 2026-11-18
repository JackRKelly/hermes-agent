"""Dispatcher concurrency caps: per-profile running counts and the opt-in fast lane.

The fast lane (``kanban.fast_lane``) exists for short, latency-bound cards such as reply drafts:
under the host caps (``max_in_progress``, ``max_in_progress_per_profile``) long builds hold every
slot for hours, and priority cannot help because builds already sit at the top of the priority
range. A fast-lane card is chosen by its ``created_by`` (who made it), never by priority, so a
high-priority build can never use the lane. Its own bound is ``max_running`` concurrent fast-lane
runs on the board; memory pressure, profile existence, the provider breaker, the respawn guard and
claim semantics still apply because every spawn goes through ``_dispatch_lane_task``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from hermes_cli.kanban_db_dispatch import DispatchResult

# Used when ``kanban.fast_lane.max_running`` is absent or not a positive integer.
DEFAULT_FAST_LANE_MAX_RUNNING = 2


@dataclass(frozen=True)
class FastLaneSettings:
    """Parsed ``kanban.fast_lane``; only built when ``created_by`` names at least one creator."""

    created_by: tuple[str, ...]
    """``tasks.created_by`` values whose ready cards may spawn beyond the caps."""
    max_running: int
    """Concurrent ``running`` fast-lane cards allowed on the board."""


def parse_fast_lane(raw: Any) -> Optional[FastLaneSettings]:
    """``kanban.fast_lane`` as settings, or ``None`` when absent, malformed or empty (feature off).

    ``created_by`` takes a list of names or one comma-separated string.
    """
    if not isinstance(raw, dict):
        return None
    names = raw.get("created_by")
    if isinstance(names, str):
        names = names.split(",")
    if not isinstance(names, (list, tuple)):
        return None
    created_by = tuple(dict.fromkeys(n.strip() for n in names if isinstance(n, str) and n.strip()))
    if not created_by:
        return None
    from hermes_cli import kanban_db_dispatch as kbd
    return FastLaneSettings(
        created_by=created_by,
        max_running=kbd._positive_int(raw.get("max_running"), DEFAULT_FAST_LANE_MAX_RUNNING),
    )


def per_profile_state(conn: sqlite3.Connection, cap: Optional[int]) -> tuple[Optional[int], dict[str, int]]:
    """``(cap, running per assignee)`` for ``kanban.max_in_progress_per_profile`` (#21582).

    A cap that is not a positive int means no per-profile cap, and then no counts are read. The
    counts are read once per tick, before any spawn, and bumped in memory per spawn so later rows
    in the same tick see this tick's spawns.
    """
    if not (isinstance(cap, int) and cap > 0):
        return None, {}
    running = {
        row["assignee"]: int(row["n"])
        for row in conn.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' AND assignee IS NOT NULL GROUP BY assignee"
        )
    }
    return cap, running


def run_fast_lane(
    conn: sqlite3.Connection,
    result: DispatchResult,
    settings: Optional[FastLaneSettings],
    lane_kwargs: dict[str, Any],
) -> frozenset[str]:
    """Spawn ready fast-lane cards ahead of, and beyond, the normal caps; returns the ids tried.

    Runs before the cap check so a full host still admits a reply draft. Ignores
    ``max_in_progress``, ``max_spawn`` and ``max_in_progress_per_profile`` (passes no per-profile
    cap), and is bounded by ``settings.max_running`` running fast-lane cards on this board. Critical
    memory pressure spawns nothing; elevated allows one spawn. Each spawn bumps
    ``lane_kwargs["per_profile_running"]`` so the normal ready loop counts it against the profile.

    The returned ids (spawned or refused) must be left out of this tick's normal ready loop: a
    refused card is already recorded on ``result`` (respawn guard, provider pause, missing profile)
    and would otherwise be recorded twice. Unassigned cards are not tried; the normal loop applies
    ``kanban.default_assignee`` to them.
    """
    if settings is None:
        return frozenset()
    from hermes_cli import kanban_db_dispatch as kbd

    marks = ",".join("?" for _ in settings.created_by)
    rows = conn.execute(
        "SELECT id, assignee FROM tasks WHERE status = 'ready' AND claim_lock IS NULL "
        f"AND assignee IS NOT NULL AND assignee != '' AND created_by IN ({marks}) "
        "ORDER BY priority DESC, created_at ASC",
        settings.created_by,
    ).fetchall()
    if not rows:
        return frozenset()
    running = int(conn.execute(
        f"SELECT COUNT(*) FROM tasks WHERE status = 'running' AND created_by IN ({marks})",
        settings.created_by,
    ).fetchone()[0])
    if running >= settings.max_running:
        return frozenset()
    pressure = kbd._memory_pressure_level()
    if pressure == "critical":
        result.memory_pressure = pressure
        return frozenset()
    pressure_limit = 1 if pressure == "elevated" else None
    if pressure_limit is not None:
        result.memory_pressure = pressure

    per_profile_running: dict[str, int] = lane_kwargs["per_profile_running"]
    kwargs = {**lane_kwargs, "per_profile_cap": None}
    tried: list[str] = []
    for row in rows:
        spawned = len(result.fast_lane_spawned)
        if running + spawned >= settings.max_running:
            break
        if pressure_limit is not None and spawned >= pressure_limit:
            break
        tried.append(row["id"])
        if kbd._dispatch_lane_task(conn, row, row["assignee"], result, lane="ready", **kwargs):
            result.fast_lane_spawned.append(row["id"])
            per_profile_running[row["assignee"]] = per_profile_running.get(row["assignee"], 0) + 1
    return frozenset(tried)


def budget_after_fast_lane(result: DispatchResult, spawn_budget: Optional[int], *, dry_run: bool) -> Optional[int]:
    """The normal lanes' spawn budget once this tick's fast-lane spawns are counted.

    A real fast-lane spawn is already ``running`` in the DB when the caps are counted, so the
    budget needs no change; a ``dry_run`` spawn claims nothing, so it is subtracted here. Under
    elevated memory pressure the tick allows one new worker in total, so a fast-lane spawn uses it.
    """
    spawned = len(result.fast_lane_spawned)
    if not spawned:
        return spawn_budget
    if result.memory_pressure == "elevated":
        return 0
    if dry_run and spawn_budget is not None:
        return max(spawn_budget - spawned, 0)
    return spawn_budget
