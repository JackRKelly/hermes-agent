"""Dispatcher concurrency caps: per-profile running counts and the opt-in fast lane.

The fast lane (``kanban.fast_lane``) exists for short, latency-bound cards such as reply drafts:
under the host caps (``max_in_progress``, ``max_in_progress_per_profile``) long builds hold every
slot for hours, and priority cannot help because builds already sit at the top of the priority
range. A fast-lane card is chosen by its ``created_by`` (who made it), never by priority, so a
high-priority build can never use the lane, and only when it was created outside the worker fleet,
so a worker cannot forge that name (:func:`_fast_lane_rows_sql`). Its own bound is
``max_running`` concurrent fast-lane runs on the board; memory pressure, profile existence, the
provider breaker, the respawn guard and claim semantics still apply because every spawn goes
through ``_dispatch_lane_task``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from hermes_cli.kanban_caps import ProfileCapPolicy
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


def per_profile_state(
    conn: sqlite3.Connection, cap: ProfileCapPolicy,
) -> tuple[Optional[ProfileCapPolicy], dict[str, int]]:
    """``(cap, running per assignee)`` for ``kanban.max_in_progress_per_profile`` and its
    per-lane overrides ``kanban.max_in_progress_by_profile`` (#21582).

    A policy with no limit for any lane means no per-profile cap: ``None``, and no counts are read.
    The counts are read once per tick, before any spawn, and bumped in memory per spawn so later
    rows in the same tick see this tick's spawns.
    """
    if not cap.active:
        return None, {}
    running = {
        row["assignee"]: int(row["n"])
        for row in conn.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' AND assignee IS NOT NULL GROUP BY assignee"
        )
    }
    return cap, running


def _fast_lane_rows_sql(settings: FastLaneSettings) -> str:
    """``WHERE`` clause (on ``tasks t``) for the cards the lane trusts; bind ``settings.created_by``.

    ``created_by`` is free text (``hermes kanban create --created-by``), so the creator name alone
    would let any worker forge a fast-lane card. A card is trusted only when its ``created`` event
    shows it was made outside the worker fleet: no ``creator_task_id`` (set by the CLI and the
    kanban tools inside a dispatched worker) and no ``worker_lineage`` (set for any create from a
    worker's process tree, see ``kanban_db_graph.created_in_worker_lineage``). A card with no
    readable ``created`` event is not trusted. The same clause bounds ``max_running``, so a forged
    card that the normal loop spawned never holds a fast-lane slot.
    """
    marks = ",".join("?" for _ in settings.created_by)
    return (
        f"t.created_by IN ({marks}) AND EXISTS (SELECT 1 FROM task_events e "
        "WHERE e.task_id = t.id AND e.kind = 'created' AND json_valid(e.payload) "
        "AND json_extract(e.payload, '$.creator_task_id') IS NULL "
        "AND json_extract(e.payload, '$.worker_lineage') IS NULL)"
    )


@dataclass(frozen=True)
class FastLaneOutcome:
    """What :func:`run_fast_lane` did this tick."""

    tried: frozenset[str] = frozenset()
    """Ids spawned or refused; the normal ready loop must skip them (already recorded)."""
    pressure: Optional[str] = None
    """The memory pressure level that limited the lane (``critical``/``elevated``), if any.

    Kept off ``result.memory_pressure`` on purpose: the tick's own cap check owns that field, and
    ``held_cap`` reads it to decide whether the cap or memory held the ready rows.
    """


def run_fast_lane(
    conn: sqlite3.Connection,
    result: DispatchResult,
    settings: Optional[FastLaneSettings],
    lane_kwargs: dict[str, Any],
) -> FastLaneOutcome:
    """Spawn ready fast-lane cards ahead of, and beyond, the normal caps.

    Runs before the cap check so a full host still admits a reply draft. Ignores
    ``max_in_progress``, ``max_spawn`` and ``max_in_progress_per_profile`` (passes no per-profile
    cap), and is bounded by ``settings.max_running`` running fast-lane cards on this board. Only
    cards created outside the worker fleet qualify (:func:`_fast_lane_rows_sql`). Critical memory
    pressure spawns nothing; elevated allows one spawn. Each spawn bumps
    ``lane_kwargs["per_profile_running"]`` so the normal ready loop counts it against the profile.

    Unassigned cards are not tried; the normal loop applies ``kanban.default_assignee`` to them.
    """
    if settings is None:
        return FastLaneOutcome()
    from hermes_cli import kanban_db_dispatch as kbd

    trusted = _fast_lane_rows_sql(settings)
    rows = conn.execute(
        "SELECT t.id, t.assignee FROM tasks t WHERE t.status = 'ready' AND t.claim_lock IS NULL "
        f"AND t.assignee IS NOT NULL AND t.assignee != '' AND {trusted} "
        "ORDER BY t.priority DESC, t.created_at ASC",
        settings.created_by,
    ).fetchall()
    if not rows:
        return FastLaneOutcome()
    running = int(conn.execute(
        f"SELECT COUNT(*) FROM tasks t WHERE t.status = 'running' AND {trusted}", settings.created_by,
    ).fetchone()[0])
    if running >= settings.max_running:
        return FastLaneOutcome()
    pressure = kbd._memory_pressure_level()
    if pressure == "critical":
        kbd._kb._log.warning(
            "kanban dispatch: memory pressure is critical; %d fast-lane card(s) wait", len(rows))
        return FastLaneOutcome(pressure=pressure)
    pressure_limit = 1 if pressure == "elevated" else None

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
    return FastLaneOutcome(tried=frozenset(tried), pressure=pressure if pressure_limit else None)


def budget_after_fast_lane(
    result: DispatchResult, spawn_budget: Optional[int], lane: FastLaneOutcome, *, dry_run: bool,
) -> Optional[int]:
    """The normal lanes' spawn budget once this tick's fast-lane spawns are counted.

    A real fast-lane spawn is already ``running`` in the DB when the caps are counted, so the
    budget needs no change; a ``dry_run`` spawn claims nothing, so it is subtracted here. Under
    elevated memory pressure the tick allows one new worker in total, so a fast-lane spawn uses it
    (the lane's own reading counts, in case the cap check's later sample disagrees).
    """
    spawned = len(result.fast_lane_spawned)
    if not spawned:
        return spawn_budget
    if lane.pressure == "elevated" or result.memory_pressure == "elevated":
        return 0
    if dry_run and spawn_budget is not None:
        return max(spawn_budget - spawned, 0)
    return spawn_budget
