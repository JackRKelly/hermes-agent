"""Dispatch health snapshot (schema 1) for ``hermes kanban health --json`` and the assistant autopilot.

The lock-holding dispatcher writes ``<db stem>.health.json`` beside ``kanban.db`` atomically at the
end of every tick (:func:`write_snapshot`); ``hermes kanban health`` re-reads the live breaker,
idle and failure-rate sections from the board and takes the ``dispatch`` section (the only part
that needs a tick's in-memory result) from that file. Field names are a contract consumed outside
this repo: change them only together with the consumer.

``bad_ticks`` counts consecutive ticks that had spawnable ready work and spawned nothing; it is
carried from the previous snapshot so it survives a per-tick ``hermes kanban dispatch`` process.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = 1
FAILURE_WINDOW_SECONDS = 3600
CLOSED_VISIBLE_SECONDS = 3600
HELD_BACK_KEYS = ("cap", "per_profile_cap", "active_pr", "provider_paused", "rate_limit_cooldown",
                  "blocker_auth", "other")
_FAILED_OUTCOMES = frozenset({"crashed", "timed_out", "spawn_failed", "gave_up", "stale"})


def snapshot_path(db_path: Path) -> Path:
    return Path(db_path).with_name(f"{Path(db_path).stem}.health.json")


def read_snapshot(db_path: Path) -> Optional[dict]:
    try:
        doc = json.loads(snapshot_path(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA else None


def providers_section(conn: sqlite3.Connection, now: int) -> list[dict]:
    from hermes_cli import kanban_db_provider_breaker as kbpb
    window = kbpb.settings().window_seconds
    out = []
    for rec in kbpb.all_breakers(conn):
        closed = rec["state"] == "closed"
        if closed and (not rec.get("closed_at") or now - int(rec["closed_at"]) > CLOSED_VISIBLE_SECONDS):
            continue
        if not rec.get("trip_id"):
            continue  # failures booked, never tripped: nothing to report
        out.append({
            "provider": rec["provider"], "state": rec["state"], "reason": rec.get("reason") or "quota",
            "message": rec.get("message") or "", "reset_at": rec.get("reset_at"),
            "opened_at": int(rec.get("opened_at") or 0), "trip_id": rec["trip_id"],
            "failures_window": sum(1 for f in rec["failures"] if isinstance(f, list) and f and f[0] >= now - window),
            "profiles": list(rec["profiles"]),
            "next_probe_at": None if closed else rec.get("next_probe_at"),
        })
    return out


def failure_rate_section(conn: sqlite3.Connection, now: int) -> list[dict]:
    from hermes_cli import kanban_db as kb
    since = now - FAILURE_WINDOW_SECONDS
    rows: dict[str, dict] = {}
    for r in conn.execute("SELECT profile, outcome, metadata, ended_at FROM task_runs "
                          "WHERE ended_at IS NOT NULL AND ended_at >= ?", (since,)):
        ended = kb._to_epoch(r["ended_at"])
        if ended is None or ended < since or not r["profile"]:
            continue
        row = rows.setdefault(r["profile"], {"profile": r["profile"], "window_seconds": FAILURE_WINDOW_SECONDS,
                                             "runs": 0, "failed": 0, "rate_limited": 0, "auth": 0})
        row["runs"] += 1
        outcome = r["outcome"] or ""
        row["failed"] += outcome in _FAILED_OUTCOMES
        row["rate_limited"] += outcome == "rate_limited"
        row["auth"] += bool(kb._json_dict(r["metadata"]).get("terminal_provider"))
    return [rows[k] for k in sorted(rows)]


def _ready_spawnable(conn: sqlite3.Connection) -> int:
    from hermes_cli import kanban_db_dispatch as kbd
    rows = conn.execute("SELECT DISTINCT assignee FROM tasks WHERE status = 'ready' AND claim_lock IS NULL "
                        "AND assignee IS NOT NULL AND assignee != ''").fetchall()
    profile_exists = kbd._profile_exists_fn()
    ok = {r["assignee"] for r in rows if profile_exists is None or profile_exists(r["assignee"])}
    if not ok:
        return 0
    marks = ",".join("?" for _ in ok)
    return int(conn.execute(f"SELECT COUNT(*) FROM tasks WHERE status = 'ready' AND claim_lock IS NULL "
                            f"AND assignee IN ({marks})", tuple(ok)).fetchone()[0])


def dispatch_section(conn: sqlite3.Connection, result: Any, previous: Optional[dict], now: int) -> dict:
    prev = (previous or {}).get("dispatch") or {}
    held = dict.fromkeys(HELD_BACK_KEYS, 0)
    held["cap"] = int(getattr(result, "held_cap", 0) or 0)
    held["per_profile_cap"] = len(getattr(result, "skipped_per_profile_capped", []) or [])
    for _task_id, reason in getattr(result, "respawn_guarded", []) or []:
        held[reason if reason in held else "other"] += 1
    ready = _ready_spawnable(conn)
    spawned = bool(getattr(result, "spawned", None))
    return {
        "ready_spawnable": ready,
        "bad_ticks": int(prev.get("bad_ticks") or 0) + 1 if ready and not spawned else 0,
        "last_spawn_at": now if spawned else prev.get("last_spawn_at"),
        "held_back": held,
    }


def build(conn: sqlite3.Connection, *, dispatch: dict, now: Optional[int] = None) -> dict:
    from hermes_cli import kanban_db_idle as kbi
    now = int(time.time()) if now is None else int(now)
    return {
        "schema": SCHEMA, "generated_at": now,
        "providers": providers_section(conn, now),
        "dispatch": dispatch,
        "idle": kbi.recent_idle(conn, since=now - FAILURE_WINDOW_SECONDS),
        "failure_rate": failure_rate_section(conn, now),
    }


def write_snapshot(conn: sqlite3.Connection, result: Any, db_path: Path, now: Optional[int] = None) -> dict:
    """Build this tick's snapshot and replace the file atomically; returns it."""
    from utils import atomic_json_write
    now = int(time.time()) if now is None else int(now)
    doc = build(conn, dispatch=dispatch_section(conn, result, read_snapshot(db_path), now), now=now)
    try:
        atomic_json_write(snapshot_path(db_path), doc)
    except OSError:
        from hermes_cli import kanban_db as kb
        kb._log.warning("kanban health: could not write %s", snapshot_path(db_path), exc_info=True)
    return doc


def live_health(conn: sqlite3.Connection, db_path: Path) -> dict:
    """``hermes kanban health``: live board sections plus the last tick's ``dispatch`` section."""
    previous = read_snapshot(db_path) or {}
    dispatch = previous.get("dispatch") or {"ready_spawnable": _ready_spawnable(conn), "bad_ticks": 0,
                                            "last_spawn_at": None, "held_back": dict.fromkeys(HELD_BACK_KEYS, 0)}
    return build(conn, dispatch=dispatch)
