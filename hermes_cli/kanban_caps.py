"""Kanban concurrency caps that change live, without a gateway restart.

Three ``config.yaml`` keys bound how many workers the dispatcher runs at once:

- ``kanban.max_in_progress``: host-wide total across every board and lane;
- ``kanban.max_in_progress_per_profile``: default limit for any one assignee profile;
- ``kanban.max_in_progress_by_profile``: ``{profile: N}`` overrides of that default.

The gateway dispatcher used to read them once at boot, so every change needed a restart.
Now it calls :meth:`CapsReloader.poll` before each tick: the config read is the mtime-cached
``load_config_readonly`` (a stat when nothing changed), a change is validated as a whole, and
a valid change applies to that very tick. Lowering a cap never touches running workers: the
dispatcher just spawns nothing new until the running count drops below it.

The caps the dispatcher actually applied are recorded in ``<kanban root>/kanban/dispatcher_caps.json``
so ``hermes kanban caps`` and the dashboard can show what is live, not only what the file says.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

# Bounds for a live change. The ceiling is a typo guard (a stray 140 for 14 would fork a
# hundred workers on one Mac); a larger fleet raises it here, in review, not by accident.
CAP_MIN = 1
CAP_MAX = 32

TOTAL_KEY = "max_in_progress"
PER_PROFILE_KEY = "max_in_progress_per_profile"
BY_PROFILE_KEY = "max_in_progress_by_profile"

LIVE_CAPS_FILENAME = "dispatcher_caps.json"


@dataclass(frozen=True)
class KanbanCaps:
    """One validated set of concurrency caps; ``None`` means the key is unset.

    ``total`` unset falls back to the memory-derived default
    (:func:`hermes_cli.kanban_db_dispatch.resolve_max_in_progress`); ``per_profile`` unset
    means no per-profile limit. ``by_profile`` is a sorted tuple so the value is hashable and
    two equal configs compare equal.
    """

    total: Optional[int] = None
    per_profile: Optional[int] = None
    by_profile: tuple[tuple[str, int], ...] = ()

    def label(self) -> str:
        """Compact ``total/per_profile`` form for logs, e.g. ``14/7 [builder=7]``."""
        total = "auto" if self.total is None else str(self.total)
        per = "-" if self.per_profile is None else str(self.per_profile)
        text = f"{total}/{per}"
        if self.by_profile:
            text += " [" + ", ".join(f"{name}={cap}" for name, cap in self.by_profile) + "]"
        return text

    def as_dict(self) -> dict[str, Any]:
        return {"total": self.total, "per_profile": self.per_profile, "by_profile": dict(self.by_profile)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> KanbanCaps:
        by_profile = data.get("by_profile") or {}
        return cls(total=data.get("total"), per_profile=data.get("per_profile"),
                   by_profile=tuple(sorted((str(k), int(v)) for k, v in by_profile.items())))


@dataclass(frozen=True)
class ProfileCapPolicy:
    """Per-assignee limit the dispatch tick enforces: an override, else the default."""

    default: Optional[int] = None
    overrides: tuple[tuple[str, int], ...] = ()

    @classmethod
    def build(cls, default: Any, overrides: Optional[Mapping[str, Any]]) -> ProfileCapPolicy:
        """Lenient constructor for ``dispatch_once`` kwargs: non-positive or non-int values drop out
        (callers validated already; a stray value must not cap a lane at 0)."""
        clean_default = default if _is_cap_int(default) else None
        clean = tuple(sorted((str(k), v) for k, v in (overrides or {}).items() if _is_cap_int(v)))
        return cls(clean_default, clean)

    @property
    def active(self) -> bool:
        """Whether any lane has a limit (so the tick must count running workers per assignee)."""
        return self.default is not None or bool(self.overrides)

    def limit(self, assignee: str) -> Optional[int]:
        for name, cap in self.overrides:
            if name == assignee:
                return cap
        return self.default


def _is_cap_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _coerce_cap(key: str, raw: Any, errors: list[str]) -> Optional[int]:
    """``raw`` as an int in ``[CAP_MIN, CAP_MAX]``; ``None`` when unset. Records an error otherwise."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        errors.append(f"{key}={raw!r} is not an integer")
        return None
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{key}={raw!r} is not an integer")
        return None
    if not CAP_MIN <= value <= CAP_MAX:
        errors.append(f"{key}={value} is outside {CAP_MIN}..{CAP_MAX}")
        return None
    return value


def parse_caps(kanban_cfg: Mapping[str, Any]) -> tuple[Optional[KanbanCaps], list[str]]:
    """Validate the three cap keys of a ``kanban`` config section as one unit.

    Returns ``(caps, [])`` when valid, ``(None, errors)`` otherwise. Rules: each value an integer
    in ``CAP_MIN..CAP_MAX``; the per-profile default and every override at most the total when
    the total is set. All-or-nothing, so a half-valid edit never applies one cap without the other.
    """
    errors: list[str] = []
    total = _coerce_cap(TOTAL_KEY, kanban_cfg.get(TOTAL_KEY), errors)
    per_profile = _coerce_cap(PER_PROFILE_KEY, kanban_cfg.get(PER_PROFILE_KEY), errors)
    raw_by = kanban_cfg.get(BY_PROFILE_KEY)
    by_profile: dict[str, int] = {}
    if raw_by is not None and not isinstance(raw_by, Mapping):
        errors.append(f"{BY_PROFILE_KEY} must be a mapping of profile name to integer")
    for name, raw in (raw_by or {}).items() if isinstance(raw_by, Mapping) else ():
        name = str(name).strip()
        if not name:
            errors.append(f"{BY_PROFILE_KEY} has an empty profile name")
            continue
        value = _coerce_cap(f"{BY_PROFILE_KEY}.{name}", raw, errors)
        if value is not None:
            by_profile[name] = value
    if total is not None:
        if per_profile is not None and per_profile > total:
            errors.append(f"{PER_PROFILE_KEY}={per_profile} is above {TOTAL_KEY}={total}")
        errors += [f"{BY_PROFILE_KEY}.{name}={cap} is above {TOTAL_KEY}={total}"
                   for name, cap in sorted(by_profile.items()) if cap > total]
    if errors:
        return None, errors
    return KanbanCaps(total, per_profile, tuple(sorted(by_profile.items()))), []


def _raw_signature(kanban_cfg: Mapping[str, Any]) -> str:
    """Stable fingerprint of the three raw values, so a change is noticed (and an invalid one
    warned about) once, not on every tick."""
    raw = {key: kanban_cfg.get(key) for key in (TOTAL_KEY, PER_PROFILE_KEY, BY_PROFILE_KEY)}
    return json.dumps(raw, sort_keys=True, default=repr)


def read_kanban_section() -> Optional[dict[str, Any]]:
    """The live ``kanban`` config section, or ``None`` when config cannot be read right now.

    ``ConfigBackendUnavailable`` is a ``SystemExit``; a tick must survive it, so it is caught
    by name next to ordinary read errors (the caller keeps the caps it has).
    """
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_backend import ConfigBackendUnavailable

    try:
        cfg = load_config_readonly() or {}
    except (Exception, ConfigBackendUnavailable):  # health: allow BLE001 -- logged; a tick keeps its caps
        logger.warning("kanban dispatcher: cannot read config for live caps; keeping current caps", exc_info=True)
        return None
    section = cfg.get("kanban") if isinstance(cfg, Mapping) else None
    return dict(section) if isinstance(section, Mapping) else {}


class CapsReloader:
    """Notices cap edits between dispatcher ticks and decides whether to apply them.

    Owned by the gateway dispatcher (one per process, used only from its loop). ``applied`` is
    what the dispatcher enforces right now.
    """

    def __init__(self, applied: KanbanCaps, boot_kanban_cfg: Mapping[str, Any]) -> None:
        self.applied = applied
        self._seen = _raw_signature(boot_kanban_cfg)

    def poll(self, kanban_cfg: Optional[Mapping[str, Any]]) -> Optional[KanbanCaps]:
        """New caps to apply, or ``None`` (unchanged, unreadable, or invalid: logged once)."""
        if kanban_cfg is None:
            return None
        signature = _raw_signature(kanban_cfg)
        if signature == self._seen:
            return None
        self._seen = signature
        caps, errors = parse_caps(kanban_cfg)
        if caps is None:
            logger.warning("kanban dispatcher: ignoring invalid caps in config.yaml (%s); keeping %s",
                           "; ".join(errors), self.applied.label())
            return None
        if caps == self.applied:
            return None
        logger.info("kanban dispatcher: caps changed %s -> %s (applied live, no restart)",
                    self.applied.label(), caps.label())
        self.applied = caps
        return caps


# --- Live record (what the dispatcher enforces) ---------------------------------------------

def live_caps_path() -> Path:
    from hermes_cli import kanban_db as kb
    return kb.kanban_home() / "kanban" / LIVE_CAPS_FILENAME


def write_live_caps(caps: KanbanCaps, *, effective_total: Optional[int], config_path: Optional[Path]) -> None:
    """Record the caps the dispatcher applied (boot and every change). Never raises: the
    record is for humans and must not stop dispatch."""
    from utils import atomic_json_write

    payload = {**caps.as_dict(), "effective_total": effective_total, "applied_at": int(time.time()),
               "config_path": str(config_path) if config_path else None}
    try:
        path = live_caps_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, payload)
    except OSError:
        logger.warning("kanban dispatcher: could not record live caps", exc_info=True)


def read_live_caps() -> Optional[dict[str, Any]]:
    """The dispatcher's last record, or ``None`` when absent or unreadable."""
    from utils import read_json_or_empty

    data = read_json_or_empty(live_caps_path())
    return data or None


# --- Writer (``hermes kanban caps set``) -----------------------------------------------------

def plan_caps_update(current: Mapping[str, Any], *, total: Optional[int] = None,
                     per_profile: Optional[int] = None, set_profiles: Optional[Mapping[str, int]] = None,
                     clear_profiles: tuple[str, ...] = ()) -> dict[str, Any]:
    """The ``kanban`` section after applying the requested edits (pure; nothing is written)."""
    section = dict(current)
    if total is not None:
        section[TOTAL_KEY] = total
    if per_profile is not None:
        section[PER_PROFILE_KEY] = per_profile
    if set_profiles or clear_profiles:
        by_profile = dict(section.get(BY_PROFILE_KEY) or {})
        by_profile.update(set_profiles or {})
        for name in clear_profiles:
            by_profile.pop(name, None)
        if by_profile:
            section[BY_PROFILE_KEY] = by_profile
        else:
            section.pop(BY_PROFILE_KEY, None)
    return section


def save_caps(**edits: Any) -> tuple[KanbanCaps, Optional[Path]]:
    """Validate and write cap edits to the current profile's ``config.yaml``.

    Takes a backup first (``backups/config/config.yaml.kanban-caps.*``), then writes through
    ``save_config`` (atomic replace, comment-preserving, fail-closed on unreadable YAML) under the
    config lock so a concurrent writer cannot interleave. Raises ``ValueError`` on invalid caps,
    leaving the file untouched. Returns the new caps and the backup path.
    """
    from hermes_cli import config as config_mod
    from hermes_cli.config_backups import backup_config

    if config_mod.is_managed():
        raise PermissionError("kanban caps cannot be changed in a managed install")
    config_path = config_mod.get_config_path()
    with config_mod._CONFIG_LOCK:
        raw = config_mod.read_user_config_raw()
        current = raw.get("kanban") if isinstance(raw.get("kanban"), Mapping) else {}
        section = plan_caps_update(current, **edits)
        caps, errors = parse_caps(section)
        if caps is None:
            raise ValueError("invalid caps: " + "; ".join(errors))
        backup = backup_config(config_path, "kanban-caps")
        raw["kanban"] = section
        keep = {("kanban", key) for key in (TOTAL_KEY, PER_PROFILE_KEY, BY_PROFILE_KEY) if key in section}
        config_mod.save_config(raw, preserve_keys=keep)
    return caps, backup


# --- Lane counts ------------------------------------------------------------------------------

def lane_counts() -> dict[str, dict[str, int]]:
    """``{assignee: {"running": n, "ready": n}}`` summed over every live board (host-wide, like the
    total cap). Boards pinned to one file by ``HERMES_KANBAN_DB`` are counted once."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    counts: dict[str, dict[str, int]] = {}
    seen: set[str] = set()
    with kb.pin_first_board_resolution():
        for meta in kb.list_boards(include_archived=False):
            slug = meta.get("slug") or kb.DEFAULT_BOARD
            path = kb.kanban_db_path(board=slug).expanduser()
            resolved = str(path.resolve())
            if resolved in seen or not path.exists():
                continue
            seen.add(resolved)
            with contextlib.closing(kbc.connect(board=slug)) as conn:
                rows = conn.execute(
                    "SELECT COALESCE(assignee, '(unassigned)') AS who, status, COUNT(*) AS n FROM tasks "
                    "WHERE status IN ('running', 'ready') GROUP BY who, status").fetchall()
            for row in rows:
                lane = counts.setdefault(row["who"], {"running": 0, "ready": 0})
                lane[row["status"]] += int(row["n"])
    return counts


# --- CLI ----------------------------------------------------------------------------------------

def _parse_profile_pairs(pairs: Optional[list[str]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for pair in pairs or ():
        name, sep, value = pair.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"--profile expects NAME=N, got {pair!r}")
        try:
            out[name.strip()] = int(value)
        except ValueError as exc:
            raise ValueError(f"--profile {pair!r}: N must be an integer") from exc
    return out


def _dispatcher_kanban_section(live: Optional[Mapping[str, Any]]) -> tuple[Path, Optional[dict[str, Any]]]:
    """The config.yaml the dispatcher reads and its ``kanban`` section.

    A dashboard or ``hermes -p other kanban caps`` can resolve a different profile than the
    gateway dispatcher; comparing the live record with that profile's file would show a
    permanent "pending" state. So when the live record names another file, read that one.
    """
    from hermes_cli.config import get_config_path
    from hermes_cli.config_backend import ConfigBackendUnavailable, read_config_doc_readonly

    own = get_config_path()
    recorded = (live or {}).get("config_path")
    if not recorded or Path(recorded).resolve() == own.resolve():
        return own, read_kanban_section()
    try:
        doc = read_config_doc_readonly(recorded) or {}
    except (Exception, ConfigBackendUnavailable):  # health: allow BLE001 -- logged; summary shows no config
        logger.warning("kanban caps: cannot read the dispatcher's config %s", recorded, exc_info=True)
        return Path(recorded), None
    section = doc.get("kanban") if isinstance(doc, Mapping) else None
    return Path(recorded), dict(section) if isinstance(section, Mapping) else {}


def caps_summary() -> dict[str, Any]:
    """Configured caps next to the dispatcher's live record (no board reads; the dashboard uses it).

    ``live`` is what the dispatcher enforces; ``in_sync`` is False while an edit waits for the next
    tick, or when config.yaml holds caps the dispatcher refused (``config_errors``).
    """
    from hermes_cli import kanban_db_dispatch as kbd

    live = read_live_caps()
    config_path, section = _dispatcher_kanban_section(live)
    caps, errors = parse_caps(section or {})
    return {
        "config_path": str(config_path),
        "configured": caps.as_dict() if caps else None,
        "config_errors": errors,
        "effective_total": kbd.resolve_max_in_progress(caps.total) if caps else None,
        "live": live,
        "in_sync": bool(live and caps and KanbanCaps.from_dict(live) == caps),
    }


def caps_snapshot() -> dict[str, Any]:
    """:func:`caps_summary` plus host-wide running/ready counts and the cap of every lane."""
    summary = caps_summary()
    configured = summary["configured"]
    caps = KanbanCaps.from_dict(configured) if configured else None
    policy = ProfileCapPolicy.build(caps.per_profile, dict(caps.by_profile)) if caps else ProfileCapPolicy()
    lanes = lane_counts()
    return {
        **summary,
        "running_total": sum(lane["running"] for lane in lanes.values()),
        "lanes": {name: {**lane, "cap": policy.limit(name)} for name, lane in sorted(lanes.items())},
    }


def _print_snapshot(snap: dict[str, Any]) -> None:
    configured, live = snap["configured"], snap["live"]
    print(f"config.yaml  {snap['config_path']}")
    if configured is None:
        print("  configured  INVALID: " + "; ".join(snap["config_errors"]))
    else:
        print(f"  configured  {KanbanCaps.from_dict(configured).label()}")
    if live is None:
        print("  live        no record yet (the gateway dispatcher writes one at start and on every change)")
    else:
        applied = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(live.get("applied_at") or 0))
        state = "in sync" if snap["in_sync"] else "differs from config.yaml: applies on the next dispatcher tick"
        print(f"  live        {KanbanCaps.from_dict(live).label()} (applied {applied}; {state})")
    total = snap["effective_total"]
    print(f"  running     {snap['running_total']}/{total if total is not None else 'uncapped'} host-wide")
    if snap["lanes"]:
        width = max(len(name) for name in snap["lanes"])
        print(f"\n  {'lane'.ljust(width)}  running  ready  cap")
        for name, lane in snap["lanes"].items():
            cap = lane["cap"] if lane["cap"] is not None else "-"
            print(f"  {name.ljust(width)}  {lane['running']:>7}  {lane['ready']:>5}  {cap:>3}")


def cmd_caps(args: argparse.Namespace) -> int:
    """``hermes kanban caps [show|set]``."""
    if getattr(args, "caps_action", None) != "set":
        snap = caps_snapshot()
        if getattr(args, "json", False):
            print(json.dumps(snap, indent=2))
        else:
            _print_snapshot(snap)
        return 0
    edits = {"total": args.total, "per_profile": args.per_profile,
             "set_profiles": _parse_profile_pairs(args.profile),
             "clear_profiles": tuple(args.clear_profile or ())}
    # `is None`/empty, not falsiness: `--total 0` is an edit that validation must refuse
    # as "outside 1..32", not "nothing to set".
    if all(v is None or v in ({}, ()) for v in edits.values()):
        raise ValueError("nothing to set: pass --total, --per-profile, --profile NAME=N or --clear-profile NAME")
    _refuse_other_profile_config()
    caps, backup = save_caps(**edits)
    print(f"kanban caps set: {caps.label()} (total/per-profile)")
    if backup:
        print(f"  backup: {backup}")
    print("  the running dispatcher applies this on its next tick; no restart needed")
    return 0


def _refuse_other_profile_config() -> None:
    """The dispatcher reads ONE config.yaml (its launch profile's). Writing another profile's
    file would look like success and change nothing, so refuse with the right file named."""
    from hermes_cli.config import get_config_path

    live = read_live_caps() or {}
    dispatcher_config = live.get("config_path")
    if dispatcher_config and Path(dispatcher_config).resolve() != get_config_path().resolve():
        raise ValueError(f"the live dispatcher reads {dispatcher_config}, not {get_config_path()}; "
                         "run `hermes kanban caps set` from that profile (no -p) instead")
