"""Dispatcher disk guard: hold spawns while the host's disk is nearly full.

A full volume turns every spawn into a failure the card did not cause: the worker log, the git
worktree and the board's own SQLite writes all fail with ENOSPC, and two such failures tripped the
circuit breaker and parked dozens of cards ``blocked``/``gave_up`` until someone unblocked each by
hand. This module keeps that from happening:

* It measures free space (``statvfs``: ``f_bavail * f_frsize``) on the kanban root, every board's
  ``default_workdir`` and ``kanban.disk_guard.extra_paths``, once per distinct volume, and takes
  the lowest reading. Readings are cached for :data:`CACHE_SECONDS`.
* Below ``heavy_min_free_gb`` the state is ``heavy_paused``: cards that would build a new tree
  (:func:`is_heavy`) are held, everything else runs. Below ``all_min_free_gb`` it is
  ``all_paused``: nothing is claimed on any lane. A state clears only once free space is back at
  least ``resume_margin_gb`` above its line, so a burst near a line does not flap.
* Every state change is written to ``<kanban root>/state/disk-guard.json`` (and one line appended
  to ``disk-guard.log.jsonl``); the assistant's owner watch reads that file to tell Jack once per
  change. The dispatcher sends no message itself.
* ENOSPC is host infrastructure, never a card failure (:func:`is_disk_full_error`): the dispatcher
  books such a failure without moving ``consecutive_failures``.
* When the disk is ``ok`` again (and once per process start), cards the breaker already parked for
  a disk-full error are unblocked once (:func:`recover_disk_gave_up`).

Stdlib only; the board helpers are imported lazily so the classifier and the guard itself load
without the kanban stack. GB here is 10**9 bytes, the unit macOS reports.
"""

from __future__ import annotations

import errno
import fnmatch
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Final, Iterable, Literal, Optional

if TYPE_CHECKING:
    from hermes_cli.kanban_db_dispatch import DispatchResult

_log = logging.getLogger(__name__)

GuardState = Literal["ok", "heavy_paused", "all_paused"]
"""``ok`` spawns everything; ``heavy_paused`` holds heavy cards; ``all_paused`` holds every card."""
OK: Final = "ok"
HEAVY_PAUSED: Final = "heavy_paused"
ALL_PAUSED: Final = "all_paused"
_STATES: Final = frozenset({OK, HEAVY_PAUSED, ALL_PAUSED})

GB: Final = 10**9
# One reading serves a whole tick (and every board in it): statvfs is cheap, but a tick may run
# many boards and the crash/spawn paths consult the state too.
CACHE_SECONDS: Final = 10.0
# Respawn-guard reason recorded for a held card (``result.respawn_guarded`` and the event).
HOLD_REASON: Final = "disk_low"
# Recovery bounds: one pass unblocks at most this many cards, and only for runs this recent, so a
# return to ``ok`` after a long-forgotten outage never mass-revives stale work.
RECOVERY_MAX_PER_PASS: Final = 100
RECOVERY_WINDOW_SECONDS: Final = 7 * 86400
_RECOVERY_OUTCOMES: Final = ("gave_up", "spawn_failed", "crashed")
# A block reason with one of these was written by a person or an agent parking the card for a
# decision; recovery never touches such a card.
_PARK_MARKERS: Final = ("[needs-jack", "[awaiting-approval", "[hold:", "[captain:")
# Events after the breaker's park that mean someone else has handled the card since.
_HANDLED_KINDS: Final = ("blocked", "unblocked", "status", "scheduled", "claimed")

STATE_FILE: Final = "disk-guard.json"
LOG_FILE: Final = "disk-guard.log.jsonl"


# --- classification of errors -------------------------------------------------------------------

# ``Errno 28`` is ENOSPC as Python prints it; SQLite reports SQLITE_FULL as "database or disk is
# full". A plain "disk I/O error" (SQLITE_IOERR) is deliberately absent: that is a failing disk or
# a bad file, which a retry once space returns would not heal.
# Hermes's own "couldn't save this conversation to disk ... The disk is probably full" is absent on
# purpose too: it is the copy for the whole ``disk`` persistence bucket
# (``hermes_state_errors.classify_persistence_error``), which also holds a read-only state.db and
# "disk I/O error". Matching it would retry such a card forever instead of parking it; a worker
# that dies with it during a real outage is infrastructure anyway, by the guard state.
_DISK_FULL_RE: Final = re.compile(r"No space left on device|database or disk is full|\bErrno 28\b", re.IGNORECASE)
# git's own wordings when it cannot write a checkout file or a ref. Each is ambiguous on its own
# (permissions, a stale lock), so they count only inside a ``git worktree add`` failure, where the
# stored error is capped at 500 chars and often loses the trailing "No space left on device".
# The ref forms are the ones git prints once the disk is too full to say why (seen on the board,
# Nov 19-20: "cannot update ref ...: couldn't write '....lock'" and "couldn't set 'refs/heads/...'").
# A bare "cannot lock ref" is deliberately absent: it also covers ref races ("reference already
# exists", "is at X but expected Y"), which a retry once space returns would not heal.
_GIT_WRITE_RE: Final = re.compile(
    r"unable to (?:create|write) file|couldn't write '[^']*\.lock'|couldn't set 'refs/",
    re.IGNORECASE)


def _text_is_disk_full(text: str) -> bool:
    if _DISK_FULL_RE.search(text):
        return True
    return "git worktree add" in text and bool(_GIT_WRITE_RE.search(text))


def is_disk_full_error(exc_or_text: BaseException | str | None) -> bool:
    """True when an exception or stored error text means the disk was full.

    An exception matches on ``errno.ENOSPC`` anywhere in its ``__cause__``/``__context__`` chain,
    or on its text; a string matches on the text: "No space left on device", "database or disk is
    full", "Errno 28", or, inside a ``git worktree add`` failure, git's "unable to create/write
    file", "couldn't write '<ref>.lock'" or "couldn't set 'refs/...'". Not a full disk: a plain
    "disk I/O error", a bare "cannot lock ref" (a ref race), or Hermes's "couldn't save this
    conversation to disk" (also said for a read-only state.db).
    """
    if exc_or_text is None:
        return False
    if isinstance(exc_or_text, str):
        return _text_is_disk_full(exc_or_text)
    seen: set[int] = set()
    exc: Optional[BaseException] = exc_or_text
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            return True
        if _text_is_disk_full(str(exc)):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


# --- settings ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DiskGuardSettings:
    """Parsed ``kanban.disk_guard``; only built when the guard is enabled."""

    heavy_min_free_gb: float = 40.0
    """Below this many GB free, heavy cards are held (``heavy_paused``)."""
    all_min_free_gb: float = 20.0
    """Below this many GB free, every card is held (``all_paused``)."""
    resume_margin_gb: float = 5.0
    """A state clears only at its line plus this margin (hysteresis)."""
    heavy_assignees: tuple[str, ...] = ("crew-pilot-*-builder",)
    """Assignee globs whose cards count as heavy whatever their workspace."""
    extra_paths: tuple[str, ...] = ()
    """More paths to measure, e.g. a repo whose worktrees live on another volume."""


def _float_setting(raw: dict, key: str, default: float) -> float:
    value = raw.get(key, default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        _log.warning("kanban disk guard: invalid kanban.disk_guard.%s=%r; using %s", key, value, default)
        return default
    if parsed < 0:
        _log.warning("kanban disk guard: kanban.disk_guard.%s=%r is negative; using %s", key, value, default)
        return default
    return parsed


def _str_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        return default
    return tuple(v.strip() for v in value if isinstance(v, str) and v.strip())


def parse_disk_guard(raw: Any) -> Optional[DiskGuardSettings]:
    """``kanban.disk_guard`` as settings, or ``None`` when ``enabled`` is false (guard off).

    An absent or malformed section means the defaults, enabled: the gateway reads the user's YAML
    without ``DEFAULT_CONFIG``, and a host that never wrote the section must still be guarded.
    """
    cfg = raw if isinstance(raw, dict) else {}
    enabled = cfg.get("enabled", True)
    if enabled is False or (isinstance(enabled, str) and enabled.strip().lower() in {"false", "0", "no", "off"}):
        return None
    base = DiskGuardSettings()
    heavy = _float_setting(cfg, "heavy_min_free_gb", base.heavy_min_free_gb)
    all_min = _float_setting(cfg, "all_min_free_gb", base.all_min_free_gb)
    if all_min > heavy:
        _log.warning("kanban disk guard: all_min_free_gb=%s is above heavy_min_free_gb=%s; "
                     "holding heavy cards from %s GB", all_min, heavy, all_min)
        heavy = all_min
    return DiskGuardSettings(
        heavy_min_free_gb=heavy,
        all_min_free_gb=all_min,
        resume_margin_gb=_float_setting(cfg, "resume_margin_gb", base.resume_margin_gb),
        heavy_assignees=_str_tuple(cfg.get("heavy_assignees"), base.heavy_assignees),
        extra_paths=_str_tuple(cfg.get("extra_paths"), base.extra_paths),
    )


def configured_disk_guard() -> Optional[DiskGuardSettings]:
    """``kanban.disk_guard`` from the current config, for dispatch entry points that do not
    already hold the kanban config (the standalone daemon, the dashboard's dispatch nudge).

    An unreadable config means the defaults, enabled, like :func:`parse_disk_guard` on a missing
    section: every entry point that spawns must be guarded the same way.
    """
    from hermes_cli.config_backend import ConfigBackendUnavailable
    try:
        from hermes_cli.config import load_config_readonly
        raw = ((load_config_readonly() or {}).get("kanban") or {}).get("disk_guard")
    except (Exception, ConfigBackendUnavailable) as exc:  # health: allow BLE001 -- logged; guarded with defaults
        _log.warning("kanban disk guard: config unreadable (%s); using the defaults", exc)
        raw = None
    return parse_disk_guard(raw)


# --- measurement and state -----------------------------------------------------------------------

def classify(free_bytes: int, previous: str, settings: DiskGuardSettings) -> GuardState:
    """The state for ``free_bytes`` given the state it is leaving (hysteresis).

    Entering a paused state uses the plain line; leaving one needs the line plus
    ``resume_margin_gb``. From ``all_paused``, a reading between the two resume points lands in
    ``heavy_paused``, never straight in ``ok``.
    """
    free_gb = free_bytes / GB
    margin = settings.resume_margin_gb
    if free_gb < settings.all_min_free_gb:
        return ALL_PAUSED
    if previous == ALL_PAUSED and free_gb < settings.all_min_free_gb + margin:
        return ALL_PAUSED
    if free_gb < settings.heavy_min_free_gb:
        return HEAVY_PAUSED
    if previous in (HEAVY_PAUSED, ALL_PAUSED) and free_gb < settings.heavy_min_free_gb + margin:
        return HEAVY_PAUSED
    return OK


def _threshold_gb(state: str, settings: DiskGuardSettings) -> float:
    """The line a state is about: the one crossed to enter it, or for ``ok`` the resume point."""
    if state == ALL_PAUSED:
        return settings.all_min_free_gb
    if state == HEAVY_PAUSED:
        return settings.heavy_min_free_gb
    return settings.heavy_min_free_gb + settings.resume_margin_gb


def _nearest_existing(path: Path) -> Path:
    """``path`` or its closest existing ancestor: a not-yet-created dir lives on its parent's volume."""
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _device_of(path: str) -> int:
    return os.stat(path).st_dev


@dataclass(frozen=True)
class DiskSnapshot:
    """The guard's view for one tick, passed to the per-card hooks."""

    state: GuardState
    free_bytes: Optional[int]
    """Lowest free space across the measured volumes; ``None`` when nothing could be measured."""
    episode_since: Optional[int]
    """Epoch second the current non-``ok`` stretch began (``None`` while ``ok``); a card gets one
    ``disk_low`` event per stretch."""
    settings: DiskGuardSettings

    @property
    def free_gb(self) -> Optional[float]:
        return None if self.free_bytes is None else round(self.free_bytes / GB, 1)


@dataclass
class DiskGuard:
    """Free-space state for one kanban root, shared by every board ticked in this process.

    ``statvfs``, ``device_of`` and ``clock`` are seams for tests (a fake disk).
    """

    root: Path
    statvfs: Callable[[str], Any] = os.statvfs
    device_of: Callable[[str], int] = _device_of
    clock: Callable[[], float] = time.time
    state: GuardState = OK
    since: Optional[int] = None
    episode_since: Optional[int] = None
    free_bytes: Optional[int] = None
    ok_epoch: int = 0
    """Bumped on every entry into ``ok`` (and on the first ``ok`` reading of the process): a board
    whose last recovery pass ran in an older epoch runs one now."""
    recovered_epoch: dict[str, int] = field(default_factory=dict)
    _read_at: Optional[float] = None
    _unwritten: Optional[dict] = None
    """A transition the disk refused to record; retried on the next fresh reading."""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    def load_persisted(self) -> None:
        """Start from the last recorded state, so a restart mid-outage keeps its hysteresis and
        its episode (no second ``disk_low`` event per card, no duplicate transition line)."""
        try:
            data = json.loads((self.state_dir / STATE_FILE).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("state") not in _STATES:
            return
        self.state = data["state"]
        self.since = _int_or_none(data.get("since"))
        self.episode_since = _int_or_none(data.get("episode_since")) if self.state != OK else None
        if self.state != OK and self.episode_since is None:
            self.episode_since = self.since

    def measure(self, paths: Iterable[str]) -> Optional[int]:
        """Lowest ``f_bavail * f_frsize`` over the distinct volumes holding ``paths``."""
        seen: set[int] = set()
        lowest: Optional[int] = None
        for raw in paths:
            try:
                probe = str(_nearest_existing(Path(raw).expanduser()))
                dev = self.device_of(probe)
                if dev in seen:
                    continue
                seen.add(dev)
                st = self.statvfs(probe)
            except OSError as exc:
                _log.debug("kanban disk guard: cannot measure %s (%s)", raw, exc)
                continue
            free = int(st.f_bavail) * int(st.f_frsize)
            lowest = free if lowest is None else min(lowest, free)
        return lowest

    def evaluate(self, settings: DiskGuardSettings, paths_fn: Callable[[], Iterable[str]], *,
                 persist: bool = True) -> DiskSnapshot:
        """The current snapshot, re-measured when the cached reading is older than
        :data:`CACHE_SECONDS`. ``persist=False`` (a dry run) measures without changing or
        recording state."""
        with self._lock:
            now = self.clock()
            if self._read_at is not None and now - self._read_at < CACHE_SECONDS:
                return self._snapshot(settings)
            free = self.measure(paths_fn())
            if free is None:
                # Nothing measurable (every path vanished or statvfs refused). Fail open: a broken
                # probe must never hold the whole board. The recorded state is kept, so the next
                # real reading still leaves a paused state through its hysteresis.
                _log.warning("kanban disk guard: no path could be measured; dispatching as ok "
                             "(recorded state %s)", self.state)
                if persist:
                    self._read_at = now  # one warning per cache window, not one per call
                    self.free_bytes = None
                return self._snapshot(settings)
            new_state = classify(free, self.state, settings)
            if not persist:
                return DiskSnapshot(new_state, free, self.episode_since or (int(now) if new_state != OK else None),
                                    settings)
            # No measured reading yet in this process (or since a stretch with none).
            first_reading = self.free_bytes is None
            self._read_at = now
            self.free_bytes = free
            if new_state != self.state:
                self._transition(new_state, free, int(now), settings)
            elif first_reading and new_state == OK:
                # A process that starts with the disk ``ok`` runs the recovery pass once: the
                # outage may have ended while no dispatcher was running.
                self.ok_epoch += 1
            elif self._unwritten is not None:
                self._write(self._unwritten)
            return self._snapshot(settings)

    def _snapshot(self, settings: DiskGuardSettings) -> DiskSnapshot:
        if self.free_bytes is None:  # unmeasured: fail open (see :meth:`evaluate`)
            return DiskSnapshot(OK, None, None, settings)
        return DiskSnapshot(self.state, self.free_bytes, self.episode_since, settings)

    def _transition(self, new_state: GuardState, free: int, now: int, settings: DiskGuardSettings) -> None:
        previous = self.state
        if new_state == OK:
            self.ok_epoch += 1
            self.episode_since = None
        elif previous == OK or self.episode_since is None:
            # heavy_paused <-> all_paused stays one episode: a card already told it is held for
            # disk is not told again.
            self.episode_since = now
        self.state = new_state
        self.since = now
        record = {
            "state": new_state,
            "free_bytes": free,
            "threshold_gb": _threshold_gb(new_state, settings),
            "since": now,
            "previous": previous,
            "episode_since": self.episode_since,
        }
        log = _log.warning if new_state != OK else _log.info
        log("kanban disk guard: %s -> %s (%.1f GB free, line %s GB)", previous, new_state, free / GB,
            record["threshold_gb"])
        self._write(record)

    def _write(self, record: dict) -> None:
        """Atomically replace the state file and append the log line. The disk may be the very
        thing that is full, so a failure is logged, kept for a retry, and never raised."""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            target = self.state_dir / STATE_FILE
            tmp = target.with_name(f".{STATE_FILE}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, target)
            with open(self.state_dir / LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as exc:
            _log.warning("kanban disk guard: could not record state %s (%s); will retry",
                         record.get("state"), exc)
            self._unwritten = record
            return
        self._unwritten = None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# One guard per kanban root for the whole process: every board on the root shares a disk, and the
# crash/spawn paths read the state of the tick that is running (``_ACTIVE``).
_GUARDS: dict[str, DiskGuard] = {}
_GUARDS_LOCK = threading.Lock()
_ACTIVE: Optional[DiskSnapshot] = None


def guard_for_root(root: Path) -> DiskGuard:
    """The process-wide guard for ``root``, created (and loaded from its state file) on first use."""
    key = str(root)
    with _GUARDS_LOCK:
        guard = _GUARDS.get(key)
        if guard is None:
            guard = DiskGuard(root=root)
            guard.load_persisted()
            _GUARDS[key] = guard
        return guard


def _measured_paths(root: Path, settings: DiskGuardSettings) -> list[str]:
    """The kanban root, every board's ``default_workdir`` (where worktrees land) and extra paths."""
    paths = [str(root)]
    try:
        from hermes_cli.kanban_db_boards import list_boards
        for meta in list_boards(include_archived=False):
            workdir = (meta.get("default_workdir") or "").strip() if isinstance(meta, dict) else ""
            if workdir:
                paths.append(workdir)
    except (OSError, sqlite3.Error, ValueError) as exc:  # the root is still measured
        _log.debug("kanban disk guard: board workdirs not listed (%s)", exc)
    paths.extend(settings.extra_paths)
    return paths


# --- dispatcher hooks ----------------------------------------------------------------------------

def begin_tick(conn: sqlite3.Connection, settings: Optional[DiskGuardSettings], *,
               dry_run: bool, guard: Optional[DiskGuard] = None) -> Optional[DiskSnapshot]:
    """Measure, record any state change and, when ``ok`` in a new epoch, run this board's recovery
    pass. Returns the tick's snapshot, or ``None`` when the guard is off (every hook is then a
    no-op and dispatch behaves as before)."""
    global _ACTIVE
    if settings is None:
        _ACTIVE = None
        return None
    from hermes_cli import kanban_db as kb
    if guard is None:
        guard = guard_for_root(kb.kanban_home())
    snapshot = guard.evaluate(settings, lambda: _measured_paths(guard.root, settings), persist=not dry_run)
    _ACTIVE = snapshot
    # Recovery needs a real ``ok`` reading: a fail-open tick (nothing measured) proves nothing.
    if snapshot.state == OK and snapshot.free_bytes is not None and not dry_run:
        board_key = _db_key(conn)
        if guard.recovered_epoch.get(board_key) != guard.ok_epoch:
            guard.recovered_epoch[board_key] = guard.ok_epoch
            try:
                recover_disk_gave_up(conn)
            except sqlite3.Error as exc:  # the board may itself be on the recovering disk
                _log.warning("kanban disk guard: recovery pass failed (%s); next ok epoch retries", exc)
    return snapshot


def _db_key(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return str(row[2] or "")
    return ""


def is_heavy(conn: sqlite3.Connection, task_id: str, assignee: str, settings: DiskGuardSettings) -> bool:
    """A card is heavy when its assignee matches ``heavy_assignees``, or it is a worktree card
    whose worktree does not exist yet: a new tree costs ~1.6 GB at checkout and ~5.6 GB once its
    dependencies are installed. An existing linked worktree (its ``.git`` is a file) is reused and
    costs nothing new. A repo-root ``workspace_path`` exists but anchors a NEW
    ``<repo>/.worktrees/<id>``, so it is heavy too."""
    if any(fnmatch.fnmatchcase(assignee or "", glob) for glob in settings.heavy_assignees):
        return True
    row = conn.execute("SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row[0] != "worktree":
        return False
    path = (row[1] or "").strip()
    if not path:
        return True
    return not (Path(path).expanduser() / ".git").is_file()


def _note_hold(conn: sqlite3.Connection, task_ids: list[str], snapshot: DiskSnapshot) -> None:
    """One ``respawn_guarded{reason: disk_low}`` event per card per guard episode."""
    from hermes_cli import kanban_db as kb
    since = snapshot.episode_since or 0
    payload = {"reason": HOLD_REASON, "free_gb": snapshot.free_gb, "state": snapshot.state}

    def _already(task_id: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'respawn_guarded' "
            "AND created_at >= ? AND json_valid(payload) "
            "AND json_extract(payload, '$.reason') = ? LIMIT 1",
            (task_id, since, HOLD_REASON),
        ).fetchone() is not None

    # Read first: on every tick after the first of an episode nothing is new, and the tick must
    # not take the board's write lock (on a full disk) just to find that out.
    fresh = [tid for tid in task_ids if not _already(tid)]
    if not fresh:
        return
    with kb.write_txn(conn):
        for task_id in fresh:
            if not _already(task_id):  # re-checked under the lock: another process may have won
                kb._append_event(conn, task_id, "respawn_guarded", payload)


def _record_holds(conn: sqlite3.Connection, task_ids: list[str], result: DispatchResult,
                  snapshot: DiskSnapshot, *, dry_run: bool) -> None:
    result.respawn_guarded.extend((tid, HOLD_REASON) for tid in task_ids)
    if not dry_run and task_ids:
        try:
            _note_hold(conn, task_ids, snapshot)
        except sqlite3.Error as exc:  # "database or disk is full": the hold itself still stands
            _log.warning("kanban disk guard: hold events not written (%s)", exc)


def hold_card(conn: sqlite3.Connection, task_id: str, assignee: str, result: DispatchResult,
              snapshot: Optional[DiskSnapshot], *, dry_run: bool) -> bool:
    """Per-card hook in ``_dispatch_lane_task``, before the respawn guard and any claim: True
    (card held, recorded as ``disk_low``) under ``all_paused``, or under ``heavy_paused`` for a
    heavy card. No failure is booked; the card stays ready."""
    if snapshot is None or snapshot.state == OK:
        return False
    if snapshot.state == HEAVY_PAUSED and not is_heavy(conn, task_id, assignee, snapshot.settings):
        return False
    _record_holds(conn, [task_id], result, snapshot, dry_run=dry_run)
    return True


def hold_all_lanes(conn: sqlite3.Connection, result: DispatchResult, snapshot: Optional[DiskSnapshot], *,
                   dry_run: bool) -> bool:
    """Tick-level hook after the reclaim/liveness passes: under ``all_paused`` record every
    unclaimed ready/review card as held and return True so the tick skips its claim loops (no
    per-card guards, workspace probes or claims while the disk is full)."""
    if snapshot is None or snapshot.state != ALL_PAUSED:
        return False
    ids = [row[0] for row in conn.execute(
        "SELECT id FROM tasks WHERE status IN ('ready', 'review') AND claim_lock IS NULL "
        "AND assignee IS NOT NULL AND assignee != '' ORDER BY priority DESC, created_at ASC")]
    _record_holds(conn, ids, result, snapshot, dry_run=dry_run)
    _log.warning("kanban disk guard: %.1f GB free; holding all %d ready/review card(s)",
                 (snapshot.free_bytes or 0) / GB, len(ids))
    return True


def spawn_failure_is_infrastructure(snapshot: Optional[DiskSnapshot], exc: BaseException) -> bool:
    """A workspace or spawn failure caused by a full disk, judged with the tick's ``snapshot``
    (``None`` = guard off: the failure counts as it always did)."""
    return snapshot is not None and is_disk_full_error(exc)


def dead_worker_is_infrastructure(conn: sqlite3.Connection, task_id: str, error_text: str) -> bool:
    """A worker that died (crash or timeout) while the disk was not ``ok``, or whose captured
    output shows a full disk, failed for the host's sake: True, and its run is stamped
    ``infrastructure`` so the respawn guard's ``infrastructure_cooldown`` spaces the retries."""
    snapshot = _ACTIVE
    if snapshot is None or not (snapshot.state != OK or is_disk_full_error(error_text)):
        return False
    from hermes_cli import kanban_db as kb
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET metadata = json_set(CASE WHEN json_valid(metadata) THEN metadata "
                "ELSE '{}' END, '$.infrastructure', json('true'), '$.disk_guard', ?) "
                "WHERE id = (SELECT id FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
                "ORDER BY ended_at DESC, id DESC LIMIT 1)",
                (snapshot.state, task_id),
            )
    except sqlite3.Error as exc:
        _log.warning("kanban disk guard: run of %s not stamped infrastructure (%s)", task_id, exc)
    _log.warning("kanban disk guard: worker of %s died with the disk %s; not counted against the card",
                 task_id, snapshot.state)
    return True


# --- recovery ------------------------------------------------------------------------------------

def _author_is_person(author: str, profile_exists: Optional[Callable[[str], bool]]) -> bool:
    """A comment author that is not a profile name is a person (fail closed: unknown = person)."""
    if profile_exists is None:
        return True
    try:
        return not profile_exists(author)
    except OSError:  # the profiles root unreadable: cannot tell, so treat it as a person
        return True


def _profile_exists() -> Optional[Callable[[str], bool]]:
    """The profile lookup (a seam for tests, which stub who counts as an agent)."""
    from hermes_cli.profiles import profile_exists
    return profile_exists


def _breaker_park_event(conn: sqlite3.Connection, task_id: str, run_started: int) -> Optional[int]:
    """Id of the ``gave_up`` event that parked the card for its latest run, or ``None`` when the
    breaker did not park it (a card blocked any other way is never ours to recover)."""
    row = conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? AND kind = 'gave_up' AND created_at >= ? "
        "ORDER BY id DESC LIMIT 1", (task_id, run_started)).fetchone()
    return None if row is None else int(row[0])


def _handled_since(conn: sqlite3.Connection, task_id: str, anchor_event_id: int,
                   profile_exists: Optional[Callable[[str], bool]]) -> bool:
    """Has a person or agent touched the card since the breaker parked it? Ordered by event id,
    not time: the claim and the spawn failure it led to usually share one second."""
    marks = ",".join("?" for _ in _HANDLED_KINDS)
    for kind, payload in conn.execute(
        f"SELECT kind, payload FROM task_events WHERE task_id = ? AND id > ? "
        f"AND (kind IN ({marks}) OR kind = 'commented')",
        (task_id, anchor_event_id, *_HANDLED_KINDS),
    ):
        if kind != "commented":
            return True
        try:
            author = str((json.loads(payload) or {}).get("author") or "")
        except (TypeError, ValueError):
            author = ""
        if _author_is_person(author, profile_exists):
            return True
    return False


def _parked_by_someone(conn: sqlite3.Connection, task_id: str, block_kind: Optional[str], since: int) -> bool:
    """The newest ``blocked`` event carries a park marker, or is a worker's ``needs_input``
    block that is not older than the run (an older one was answered before the run started)."""
    row = conn.execute(
        "SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind = 'blocked' "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
    if row is None:
        return False
    reason = ""
    try:
        reason = str((json.loads(row[0]) or {}).get("reason") or "")
    except (TypeError, ValueError):
        pass
    if row[1] >= since and (block_kind == "needs_input" or any(m in reason for m in _PARK_MARKERS)):
        return True
    return False


def recover_disk_gave_up(conn: sqlite3.Connection, *, now: Optional[float] = None,
                         limit: int = RECOVERY_MAX_PER_PASS,
                         window_seconds: int = RECOVERY_WINDOW_SECONDS) -> list[str]:
    """Unblock, once, the ``blocked`` cards the breaker parked for a full disk.

    A card qualifies when its latest ended run (within ``window_seconds``) ended ``gave_up``,
    ``spawn_failed`` or ``crashed`` with a disk-full error (:func:`is_disk_full_error`), the
    breaker's ``gave_up`` event parked it for that run, and nobody handled it since: no
    ``blocked``/``unblocked``/``status``/``scheduled``/``claimed`` event and no comment by a person
    after that ``gave_up``, and no park marker (``[needs-jack``,
    ``[awaiting-approval``, ``[hold:``, ``[captain:``) or worker ``needs_input`` block. Each goes
    back to its retry status through :func:`kanban_db.unblock_task` (which resets
    ``consecutive_failures``) with an ``unblocked{reason: disk_recovered}`` event; that event is
    newer than the park, so the same card is never recovered twice for it. At most ``limit`` per
    pass. Returns the unblocked ids.
    """
    from hermes_cli import kanban_db as kb
    cutoff = int((now if now is not None else time.time()) - window_seconds)
    rows = conn.execute(
        "SELECT t.id, t.block_kind, t.last_failure_error, r.outcome, r.error, r.started_at, r.ended_at "
        "FROM tasks t JOIN task_runs r ON r.id = ("
        "  SELECT id FROM task_runs WHERE task_id = t.id AND ended_at IS NOT NULL "
        "  ORDER BY ended_at DESC, id DESC LIMIT 1) "
        "WHERE t.status = 'blocked' AND r.ended_at >= ? "
        f"AND r.outcome IN ({','.join('?' for _ in _RECOVERY_OUTCOMES)}) "
        "ORDER BY r.ended_at DESC",
        (cutoff, *_RECOVERY_OUTCOMES),
    ).fetchall()
    profile_exists = _profile_exists()
    recovered: list[str] = []
    for task_id, block_kind, last_error, _outcome, run_error, started_at, ended_at in rows:
        if len(recovered) >= limit:
            break
        if not (is_disk_full_error(run_error or "") or is_disk_full_error(last_error or "")):
            continue
        run_started = int(kb._to_epoch(started_at) or kb._to_epoch(ended_at) or 0)
        anchor = _breaker_park_event(conn, task_id, run_started)
        if anchor is None or _handled_since(conn, task_id, anchor, profile_exists):
            continue
        if _parked_by_someone(conn, task_id, block_kind, run_started):
            continue
        if kb.unblock_task(conn, task_id, reason="disk_recovered"):
            recovered.append(task_id)
    if recovered:
        _log.warning("kanban disk guard: disk is ok; unblocked %d card(s) parked for a full disk: %s",
                     len(recovered), ", ".join(recovered))
    return recovered


# --- CLI: a dry reading ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    """``python -m hermes_cli.kanban_disk_guard [--root DIR]``: print the state the guard would
    report for the kanban root right now, without writing anything."""
    import argparse
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--root", help="kanban root (default: the configured kanban home)")
    args = parser.parse_args(argv)
    if args.root:
        root = Path(args.root).expanduser()
    else:
        from hermes_cli import kanban_db as kb
        root = kb.kanban_home()
    from hermes_cli.config import load_config
    cfg = load_config()
    parsed = parse_disk_guard(((cfg or {}).get("kanban") or {}).get("disk_guard"))
    # Disabled still gets a reading at the default lines, labelled as such.
    settings = parsed or DiskGuardSettings()
    guard = DiskGuard(root=root)
    guard.load_persisted()
    paths = _measured_paths(root, settings)
    snapshot = guard.evaluate(settings, lambda: paths, persist=False)
    print(json.dumps({"enabled": parsed is not None, "root": str(root), "paths": paths, "state": snapshot.state,
                      "free_gb": snapshot.free_gb, "recorded_state": guard.state,
                      "heavy_min_free_gb": settings.heavy_min_free_gb,
                      "all_min_free_gb": settings.all_min_free_gb,
                      "resume_margin_gb": settings.resume_margin_gb}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
