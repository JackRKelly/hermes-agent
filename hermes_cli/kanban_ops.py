"""Dispatcher and maintenance verbs for ``hermes kanban``: ``dispatch``,
``daemon`` (deprecated standalone loop), ``tail``/``watch`` event streaming,
``gc`` and ``repair``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_cli import kanban_caps as kcaps
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli.kanban_db_dispatch_caps import parse_fast_lane
from hermes_cli.kanban_db_health import describe_suppression
from hermes_cli.kanban_disk_guard import parse_disk_guard
from hermes_cli.kanban_output import _err, _fmt_ts, _print_json


def _kanban_config() -> dict:
    """``config.yaml`` ``kanban:`` section, or ``{}`` when config can't be loaded."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return (cfg.get("kanban", {}) if isinstance(cfg, dict) else {}) or {}
    except Exception:
        return {}


def _poll_loop(interval: float, tick) -> int:
    """Run ``tick()`` every ``interval`` seconds (floor 0.1) until Ctrl-C."""
    try:
        while True:
            tick()
            time.sleep(max(0.1, interval))
    except KeyboardInterrupt:
        print("\n(stopped)")
        return 0


def _cmd_tail(args: argparse.Namespace) -> int:
    last_id = 0
    print(f"Tailing events for {args.task_id}. Ctrl-C to stop.")

    def tick():
        nonlocal last_id
        with kbc.connect_closing() as conn:
            events = kb.list_events(conn, args.task_id)
        for e in events:
            if e.id > last_id:
                pl = f" {e.payload}" if e.payload else ""
                print(f"[{_fmt_ts(e.created_at)}] {e.kind}{pl}", flush=True)
                last_id = e.id

    return _poll_loop(args.interval, tick)


def _dispatcher_kanban_config() -> tuple[Optional[Path], dict[str, Any]]:
    """``(path, kanban section)`` of the config the gateway dispatcher reads.

    The concurrency caps bound the HOST, so ``hermes kanban dispatch`` must apply the same
    set the gateway's embedded dispatcher enforces — whatever profile invokes it. The
    dispatcher records its config path in the live-caps record (written at boot;
    ``read_live_caps_record`` may return the record of a dispatcher that is gone). Without
    a record, the default home's ``config.yaml`` is what a gateway would have launched
    with. Only then fall back to the invoking profile's own file — the historical
    behaviour, and the path that let a pass from a profile without a ``kanban:`` section
    spawn 78 workers past the host cap (Nov 27, 2026).
    """
    from hermes_cli.config_backend import ConfigBackendUnavailable, read_config_doc_readonly

    record = kcaps.read_live_caps_record()
    if (record or {}).get("config_path"):
        path, section = kcaps._dispatcher_kanban_section(record)
        if section is not None:
            return path, section
        # The recorded file is unreadable right now (moved/deleted home): fall through.
    home_config = kb.kanban_home() / "config.yaml"
    try:
        if home_config.exists():
            doc = read_config_doc_readonly(home_config) or {}
            section = doc.get("kanban") if isinstance(doc, Mapping) else None
            if isinstance(section, Mapping):
                return home_config, dict(section)
    except (Exception, ConfigBackendUnavailable):  # health: allow BLE001 -- try the next candidate
        pass
    path, section = kcaps._dispatcher_kanban_section(None)
    return path, section or {}


def _positive_or_none(value: Any) -> Optional[int]:
    """``value`` as a positive integer, else ``None`` (the gateway's lenient cap parse,
    ``gateway/kanban_watchers_common.py::_positive_int_setting``: unset or invalid -> no cap)."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


def _dispatch_caps(section: Mapping[str, Any]) -> kcaps.KanbanCaps:
    """The three cap keys, validated exactly like the gateway's boot path.

    ``kanban_caps.parse_caps`` is all-or-nothing (1..32, per-lane caps at most the total).
    On a failing set the gateway boots on the plain integer values with no per-lane
    overrides (``gateway/kanban_watchers_dispatcher.py::_boot_caps``); mirroring that keeps
    this pass on the same cap set the running dispatcher enforces, instead of silently
    dropping every cap in a half-valid file.
    """
    caps, errors = kcaps.parse_caps(section)
    if caps is not None:
        return caps
    print("warning: kanban caps in the dispatcher's config fail validation "
          f"({'; '.join(errors)}); applying the plain integer values like the gateway boot",
          file=sys.stderr)
    return kcaps.KanbanCaps(
        total=_positive_or_none(section.get(kcaps.TOTAL_KEY)),
        per_profile=_positive_or_none(section.get(kcaps.PER_PROFILE_KEY)),
    )


def _caps_line(caps: kcaps.KanbanCaps, effective_total: Optional[int], source: Optional[Path]) -> str:
    """One line naming the caps this pass applied and the config file they came from."""
    total = "uncapped" if effective_total is None else str(effective_total)
    per = "no per-profile cap" if caps.per_profile is None else f"{caps.per_profile} per profile"
    by = ""
    if caps.by_profile:
        by = " [" + ", ".join(f"{name}={cap}" for name, cap in caps.by_profile) + "]"
    src = f" ({source})" if source else ""
    return f"Caps: {total} total / {per}{by}{src}"


def _cmd_dispatch(args: argparse.Namespace) -> int:
    # Honour kanban.default_assignee, kanban.max_in_progress,
    # kanban.max_in_progress_per_profile, kanban.fast_lane and kanban.max_spawn with the same
    # semantics as the gateway dispatch path — read from the config the gateway dispatcher
    # reads, so a pass from another profile applies the same host caps
    # (_dispatcher_kanban_config).
    ignore_caps = bool(getattr(args, "ignore_caps", False))
    caps = kcaps.KanbanCaps()
    caps_path: Optional[Path] = None
    effective_total: Optional[int] = None
    default_assignee = None
    max_spawn_from_config = None
    fast_lane = None
    try:
        caps_path, section = _dispatcher_kanban_config()
        caps = _dispatch_caps(section)
        # Memory-derived default when unset — same fallback the gateway applies.
        effective_total = kbd.resolve_max_in_progress(caps.total)
        default_assignee = (section.get("default_assignee") or "").strip() or None
        max_spawn_from_config = _positive_or_none(section.get("max_spawn"))
        fast_lane = parse_fast_lane(section.get("fast_lane"))
        disk_guard = parse_disk_guard(section.get("disk_guard"))
    except Exception:
        disk_guard = parse_disk_guard(None)  # unreadable config: guarded with the defaults
    max_in_progress = effective_total
    max_in_progress_per_profile = caps.per_profile
    # Per-lane overrides; dispatch_once drops non-positive entries.
    max_in_progress_by_profile = dict(caps.by_profile) or None
    # CLI --max is the more explicit signal, so it wins over kanban.max_spawn.
    cli_max = getattr(args, "max", None)
    max_spawn = cli_max if cli_max is not None else max_spawn_from_config
    if ignore_caps:
        # One deliberate pass past the caps for an operator who takes responsibility; --max,
        # the respawn guards, the provider breaker and the memory and disk guards still bound it.
        max_in_progress = max_in_progress_per_profile = max_in_progress_by_profile = None
        print("warning: --ignore-caps: ignoring the total and per-profile caps for this pass "
              "(still bounded by --max)", file=sys.stderr)
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn,
            dry_run=args.dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=getattr(args, "failure_limit", kbd.DEFAULT_FAILURE_LIMIT),
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
            max_in_progress_by_profile=max_in_progress_by_profile,
            fast_lane=fast_lane,
            disk_guard=disk_guard,
        )
    if getattr(args, "json", False):
        _print_json({
            **{k: getattr(res, k)
               for k in ("reclaimed", "crashed", "timed_out", "stale", "auto_blocked", "promoted",
                         "reaped_terminal_workers")},
            "spawned": [
                {"task_id": tid, "assignee": who, "workspace": ws} for (tid, who, ws) in res.spawned
            ],
            "fast_lane_spawned": res.fast_lane_spawned,
            "skipped_unassigned": res.skipped_unassigned,
            "skipped_nonspawnable": res.skipped_nonspawnable,
            "skipped_per_profile_capped": [
                {"task_id": tid, "assignee": who, "current": current}
                for (tid, who, current) in res.skipped_per_profile_capped
            ],
            "auto_assigned_default": res.auto_assigned_default,
            "respawn_guarded": [
                {"task_id": tid, "reason": reason}
                for (tid, reason) in res.respawn_guarded
            ],
            "rate_limited": res.rate_limited,
            "skipped_locked": res.skipped_locked,
            "memory_pressure": res.memory_pressure,
            "caps": {**caps.as_dict(), "effective_total": effective_total,
                     "source": str(caps_path) if caps_path else None},
            "held_back": {"cap": res.held_cap,
                          "per_profile_cap": len(res.skipped_per_profile_capped)},
            "caps_ignored": ignore_caps,
        }, ascii=True)
        return 0
    print(_caps_line(caps, effective_total, caps_path))
    print(f"Reclaimed:    {res.reclaimed}")
    if res.reaped_terminal_workers:
        print(f"Reaped workers of finished tasks: {', '.join(res.reaped_terminal_workers)}")
    for label, items in (
        ("Crashed:     ", res.crashed),
        ("Timed out:   ", res.timed_out),
        ("Stale:       ", res.stale),
        ("Auto-blocked:", res.auto_blocked),
    ):
        print(f"{label} {len(items)}")
        if items:
            print(f"  {', '.join(items)}")
    print(f"Promoted:     {res.promoted}")
    print(f"Spawned:      {len(res.spawned)}")
    tag = " (dry)" if args.dry_run else ""
    for tid, who, ws in res.spawned:
        print(f"  - {tid}  ->  {who}  @ {ws or '-'}{tag}")
    if res.fast_lane_spawned:
        print(f"Fast lane (past the caps): {', '.join(res.fast_lane_spawned)}")
    if res.auto_assigned_default:
        print(
            f"Auto-assigned to kanban.default_assignee={default_assignee!r}: "
            f"{', '.join(res.auto_assigned_default)}"
        )
    if res.skipped_unassigned:
        print(f"Skipped (unassigned): {', '.join(res.skipped_unassigned)}")
    for tid, who, current in res.skipped_per_profile_capped:
        print(f"Deferred ({who} at per-profile cap, {current} running): {tid}")
    if res.skipped_nonspawnable:
        print(
            f"Skipped (non-spawnable assignee — terminal lane, OK): "
            f"{', '.join(res.skipped_nonspawnable)}"
        )
    for tid, reason in res.respawn_guarded:
        print(f"Guarded ({reason}): {tid}")
    if res.rate_limited:
        print(f"Rate-limited (released to ready, no failure counted): {', '.join(res.rate_limited)}")
    if res.skipped_locked:
        print("Skipped: another dispatcher holds this board's lock (no writes this tick)")
    if res.memory_pressure:
        print(f"Memory pressure {res.memory_pressure}: new workers restricted this tick")
    held_profile = len(res.skipped_per_profile_capped)
    if res.held_cap or held_profile:
        print(f"Held back by caps: {res.held_cap + held_profile} (host cap: {res.held_cap}, "
              f"per-profile caps: {held_profile}); --ignore-caps exceeds them")
    return 0


_DAEMON_DEPRECATED = (
    "hermes kanban daemon: DEPRECATED — the dispatcher now runs\ninside the gateway. To use "
    "kanban:\n\n    hermes gateway start       # starts the gateway + embedded dispatcher\n\nReady "
    "tasks will be picked up on the next dispatcher tick\n(default: every 60 seconds). Configure "
    "via config.yaml:\n\n    kanban:\n      dispatch_in_gateway: true      # default\n      "
    "dispatch_interval_seconds: 60\n      failure_limit: 2              # consecutive non-success "
    "attempts before auto-block\n\nRunning both the gateway AND this standalone daemon will\nrace "
    "for claims. If you truly need the old standalone\ndaemon (no gateway available), rerun with "
    "--force."
)


def _cmd_daemon(args: argparse.Namespace) -> int:
    """Deprecated — the dispatcher now runs inside the gateway. Kept so old
    scripts/systemd units get a clear migration message; ``--force`` (hidden
    from --help) keeps the standalone loop for hosts that truly cannot run the
    gateway. The default path exits 2 so nobody accidentally runs two
    dispatchers against the same kanban.db."""
    if not getattr(args, "force", False):
        return _err(_DAEMON_DEPRECATED, 2)

    # Init before printing "started" so the DB path is right and init errors
    # surface immediately.
    kb.init_db()

    pidfile = getattr(args, "pidfile", None)
    if pidfile:
        try:
            Path(pidfile).parent.mkdir(parents=True, exist_ok=True)
            Path(pidfile).write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            print(f"warning: could not write pidfile {pidfile}: {exc}", file=sys.stderr)

    verbose = bool(getattr(args, "verbose", False))
    print(
        f"Kanban dispatcher running STANDALONE via --force (interval={args.interval}s, "
        f"pid={os.getpid()}). Ctrl-C to stop. NOTE: if a gateway is also running with "
        f"dispatch_in_gateway=true (default), you have two dispatchers racing for claims.",
        file=sys.stderr,
    )

    # Health telemetry: warn when every tick finds ready work but spawns
    # nothing (broken profile, PATH drift, missing venv, credential loss) —
    # the per-task breaker auto-blocks quietly, so the operator needs a signal.
    HEALTH_WINDOW = 6  # ticks (default 30s at interval=5)
    health_state = {"bad_ticks": 0, "last_warn_at": 0}

    def _ready_queue_nonempty() -> bool:
        """Is there a ready+assigned+unclaimed task the dispatcher would spawn for?
        Control-plane lanes pulled via ``claim_task`` are correctly idle, not stuck."""
        try:
            with kbc.connect_closing() as conn:
                return kbd.has_spawnable_ready(conn)
        except Exception:
            return False

    def _on_tick(res):
        ready_pending = bool(res.skipped_unassigned) or _ready_queue_nonempty()
        if ready_pending and not res.spawned:
            health_state["bad_ticks"] += 1
        else:
            health_state["bad_ticks"] = 0
        # Warn once per HEALTH_WINDOW bad ticks, at most every 5 minutes.
        if health_state["bad_ticks"] >= HEALTH_WINDOW:
            now = int(time.time())
            if now - health_state["last_warn_at"] >= 300:
                held = describe_suppression([res])
                held = f" Last tick held back: {held}." if held else ""
                print(
                    f"[{_fmt_ts(now)}] WARN dispatcher stuck: ready queue non-empty for "
                    f"{health_state['bad_ticks']} consecutive ticks but 0 workers spawned "
                    f"successfully.{held} Check profile health (venv, PATH, credentials) and `hermes "
                    f"kanban list --status ready` / `hermes kanban list --status blocked` for "
                    f"recent spawn_failed tasks.",
                    file=sys.stderr, flush=True,
                )
                health_state["last_warn_at"] = now
        if not verbose:
            return
        did_work = (
            res.reclaimed or res.crashed or res.timed_out or res.promoted
            or res.spawned or res.auto_blocked or res.stale
        )
        if did_work:
            print(
                f"[{_fmt_ts(int(time.time()))}] reclaimed={res.reclaimed} "
                f"crashed={len(res.crashed)} timed_out={len(res.timed_out)} stale={len(res.stale)} "
                f"promoted={res.promoted} spawned={len(res.spawned)} "
                f"auto_blocked={len(res.auto_blocked)}",
                flush=True,
            )

    try:
        kbd.run_daemon(
            interval=args.interval,
            max_spawn=args.max,
            failure_limit=getattr(args, "failure_limit", kbd.DEFAULT_FAILURE_LIMIT),
            on_tick=_on_tick,
        )
    finally:
        if pidfile:
            try:
                Path(pidfile).unlink()
            except OSError:
                pass
    print("(dispatcher stopped)")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Live-stream task_events to the terminal."""
    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()} if args.kinds else None
    print(f"Watching kanban events (initial board '{kb.get_current_board()}'). Ctrl-C to stop.", flush=True)
    # Seed cursor at the latest id so we don't replay history.
    with kbc.connect_closing() as conn:
        cursor = int(conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()["m"])

    def tick():
        nonlocal cursor
        with kbc.connect_closing() as conn:
            rows = conn.execute(
                "SELECT e.id, e.task_id, e.kind, e.payload, e.created_at,        t.assignee, "
                "t.tenant FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id WHERE e.id > ? "
                "ORDER BY e.id ASC LIMIT 200",
                (cursor,),
            ).fetchall()
        for r in rows:
            cursor = max(cursor, int(r["id"]))
            if (kinds and r["kind"] not in kinds) or (args.assignee and r["assignee"] != args.assignee) \
                    or (args.tenant and r["tenant"] != args.tenant):
                continue
            try:
                payload = json.loads(r["payload"]) if r["payload"] else None
            except Exception:
                payload = None
            pl = f" {payload}" if payload else ""
            print(
                f"[{_fmt_ts(kb._to_epoch(r['created_at']))}] {r['task_id']:10s} "
                f"{r['kind']:18s} (@{r['assignee'] or '-'}){pl}",
                flush=True,
            )

    return _poll_loop(args.interval, tick)


def _cmd_gc(args: argparse.Namespace) -> int:
    """Remove archived tasks' scratch workspaces, old events, and old worker logs."""
    import shutil
    event_days = getattr(args, "event_retention_days", 30)
    log_days = getattr(args, "log_retention_days", 30)
    if event_days < 0 or log_days < 0:
        return _err("kanban gc: retention days must be >= 0 (0 disables that sweep)", 2)
    scratch_root = kb.workspaces_root()
    removed_ws = 0
    with kbc.connect_closing() as conn:
        rows = conn.execute(
            "SELECT id, workspace_kind, workspace_path, branch_name FROM tasks "
            "WHERE status = 'archived'"
        ).fetchall()
        for row in rows:
            if row["workspace_kind"] == "worktree":
                # Backstop for worktrees that escaped the completion/archive hook.
                # Same safety predicate: only clean, fully-pushed worktrees go.
                wt_path = row["workspace_path"]
                if wt_path and Path(wt_path).is_dir():
                    if kbw._defer_shared_worktree_cleanup(conn, row["id"], wt_path):
                        continue
                    kbw._cleanup_worktree_workspace(row["id"], wt_path, row["branch_name"])
                    if not Path(wt_path).is_dir():
                        removed_ws += 1
                continue
            if row["workspace_kind"] != "scratch":
                continue
            path = Path(row["workspace_path"] or (scratch_root / row["id"]))
            # Same containment predicate as completion cleanup (#28818): strictly below a
            # managed root, never the root itself (which holds every task's scratch dir).
            # Cheap existence/symlink check first: most rows were already cleaned at
            # completion, and rmtree refuses a symlink (so it must not be counted).
            if not path.is_dir() or path.is_symlink() or not kbw._is_managed_scratch_path(path):
                continue
            if kbw._defer_shared_workspace_cleanup(conn, row["id"], path):
                continue
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                removed_ws += 1

    removed_events = 0
    if event_days:
        with kbc.connect_closing() as conn:
            removed_events = kb.gc_events(conn, older_than_seconds=event_days * 24 * 3600)
    removed_logs = kb.gc_worker_logs(older_than_seconds=log_days * 24 * 3600) if log_days else 0
    print(f"GC complete: {removed_ws} workspace(s), "
          f"{removed_events} event row(s), {removed_logs} log file(s) removed")
    return 0


def _cmd_repair(args: argparse.Namespace) -> int:
    """Integrity check + narrow index-REINDEX auto-repair. Dispatched BEFORE
    the auto ``kb.init_db()`` (init refuses corrupt DBs). Exit 0 = healthy /
    repaired / no DB file, 1 = still corrupt."""
    try:
        report = kbc.repair_db()
    except Exception as exc:  # locked/busy probe, unexpected I/O
        return _err(f"kanban repair: {exc}")

    if getattr(args, "json", False):
        _print_json({
            "status": report.status,
            "db_path": str(report.db_path),
            "messages": report.messages,
            "post_repair_messages": report.post_repair_messages,
            "backup_path": str(report.backup_path) if report.backup_path else None,
            "reindexed": report.reindexed,
        }, ascii=True)
        return 0 if report.status in {"ok", "repaired", "missing"} else 1

    if report.status == "missing":
        print(f"No kanban DB at {report.db_path} — nothing to repair.")
        return 0
    if report.status == "ok":
        print(f"{report.db_path}: integrity_check ok — no repair needed.")
        return 0
    if report.status == "repaired":
        print(f"{report.db_path}: repaired.")
        print(f"  reindexed: {', '.join(report.reindexed)}")
        if report.backup_path:
            print(f"  pre-repair backup: {report.backup_path}")
        print("  integrity_check now ok.")
        return 0
    # still corrupt
    def err(line: str) -> None:
        print(line, file=sys.stderr)

    err(f"{report.db_path}: CORRUPT.")
    for line in (report.messages or [])[:10]:
        err(f"  {line}")
    if report.reindexed:
        err(f"  REINDEX ({', '.join(report.reindexed)}) attempted but integrity_check is still failing:")
        for line in (report.post_repair_messages or [])[:10]:
            err(f"    {line}")
    else:
        err("  Not an index-only failure — automatic REINDEX repair does not apply (fail-closed).")
    if report.backup_path:
        err(f"  corrupt copy quarantined at: {report.backup_path}")
    err(
        "  Recover manually (copy kanban.db aside FIRST, then run "
        "`sqlite3 <copy> \".recover\"` into a fresh file — never against "
        "the live path, a WAL-reset-vulnerable sqlite3 CLI can corrupt it "
        "further) or move the file aside to start a new board."
    )
    return 1
