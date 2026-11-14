"""Provider health breaker for the kanban dispatcher.

A quota wall (exit 75) or a lost login (exit 78) is a property of the PROVIDER, not of one card:
every profile whose ``model.provider`` resolves to it hits the same wall. The per-card
``rate_limit_cooldown`` only spaces one card's retries, so N cards still respawned into a dead
provider every five minutes. This breaker is keyed by the assignee profile's resolved
``model.provider`` (the profile name when that is unresolvable) and persisted in ``kanban.db``
(``provider_breakers``) so it survives a gateway restart.

States: ``closed`` (normal), ``open`` (nothing of that provider spawns: held back as
``provider_paused``), ``half_open`` (exactly one ready card may spawn as a probe every
``probe_seconds``). Trip: ``failures`` exits 75/78 of one provider within ``window_seconds``.
A probe run that survives ``probe_seconds`` or ends any other way closes the breaker; a probe that
exits 75/78 re-opens it under the same ``trip_id``. Probes wait for a parsed ``reset_at``, but
never longer than ``max_probe_wait_seconds`` (a quota wall that lifts early, or a login swapped
somewhere the fingerprint cannot see, costs one probe per interval, not days). A credential
fingerprint change (an account added or re-logged) half-opens at once. An operator can force a
probe (:func:`probe_now`) or close a breaker (:func:`reset_breaker`): ``hermes kanban health
--probe/--reset <provider>``.

While the breaker is not closed an exit 78 is provider-wide, so it is booked like a rate-limited
requeue instead of parking the card; a trip on auth also releases the cards a lone 78 parked inside
the window. A lone 78 with no breaker still parks (``_account_crashes``).
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

BREAKER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS provider_breakers (
    provider         TEXT PRIMARY KEY,
    state            TEXT NOT NULL DEFAULT 'closed',
    reason           TEXT,
    message          TEXT,
    reset_at         INTEGER,
    opened_at        INTEGER,
    closed_at        INTEGER,
    trip_id          TEXT,
    failures         TEXT NOT NULL DEFAULT '[]',
    profiles         TEXT NOT NULL DEFAULT '[]',
    next_probe_at    INTEGER,
    probe_task_id    TEXT,
    probe_started_at INTEGER,
    fingerprint      TEXT,
    updated_at       INTEGER
);
"""

DEFAULT_FAILURES = 3
DEFAULT_WINDOW_SECONDS = 300
DEFAULT_PROBE_SECONDS = 90
DEFAULT_MAX_PROBE_WAIT_SECONDS = 1800
MESSAGE_MAX_CHARS = 200

_EXIT_KINDS = {"rate_limited": "quota", "terminal_provider": "auth"}


@dataclass(frozen=True)
class BreakerSettings:
    failures: int = DEFAULT_FAILURES
    window_seconds: int = DEFAULT_WINDOW_SECONDS
    probe_seconds: int = DEFAULT_PROBE_SECONDS
    max_probe_wait_seconds: int = DEFAULT_MAX_PROBE_WAIT_SECONDS


def _positive(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def settings() -> BreakerSettings:
    """``kanban.provider_breaker.{failures,window_seconds,probe_seconds,max_probe_wait_seconds}``
    (defaults 3/300/90/1800)."""
    from hermes_cli.config_backend import ConfigBackendUnavailable
    try:
        from hermes_cli.config import load_config
        section = ((load_config() or {}).get("kanban") or {}).get("provider_breaker") or {}
    except (ConfigBackendUnavailable, OSError, ValueError, TypeError, AttributeError):
        section = {}
    if not isinstance(section, dict):
        section = {}
    return BreakerSettings(
        failures=_positive(section.get("failures"), DEFAULT_FAILURES),
        window_seconds=_positive(section.get("window_seconds"), DEFAULT_WINDOW_SECONDS),
        probe_seconds=_positive(section.get("probe_seconds"), DEFAULT_PROBE_SECONDS),
        max_probe_wait_seconds=_positive(section.get("max_probe_wait_seconds"), DEFAULT_MAX_PROBE_WAIT_SECONDS),
    )


# --------------------------------------------------------------------------- profile -> provider

_provider_cache: dict[str, tuple[Any, str]] = {}


def _profile_home(profile: str) -> Optional[Path]:
    from hermes_cli.profiles import resolve_profile_env
    try:
        return Path(resolve_profile_env(profile))
    except (ValueError, OSError):  # invalid name / missing profile (FileNotFoundError)
        return None


def provider_for_profile(profile: str) -> str:
    """The profile's ``model.provider``, else the profile name (its own breaker)."""
    name = (profile or "").strip()
    if not name:
        return ""
    home = _profile_home(name)
    if home is None:
        return name
    from hermes_cli.config_backend import (
        ConfigBackendUnavailable, config_cache_version_or_none, read_config_doc_readonly,
    )
    path = home / "config.yaml"
    try:
        version = config_cache_version_or_none(path)
        cached = _provider_cache.get(name)
        if cached is not None and cached[0] == version and version is not None:
            return cached[1]
        doc = read_config_doc_readonly(path) if version is not None else None
    except (ConfigBackendUnavailable, OSError, ValueError, TypeError):
        return name
    model = doc.get("model") if isinstance(doc, dict) else None
    provider = model.get("provider") if isinstance(model, dict) else None
    resolved = str(provider).strip() if isinstance(provider, str) and provider.strip() else name
    _provider_cache[name] = (version, resolved)
    return resolved


def _stat_part(label: str, path: Optional[Path]) -> str:
    try:
        st = path.stat() if path is not None else None
    except OSError:
        st = None
    return f"{label}:{st.st_mtime_ns}:{st.st_size}" if st else f"{label}:-"


def _claude_login_path() -> Optional[Path]:
    """Claude Code's shared OAuth file (``CLAUDE_CONFIG_DIR`` aware), the login every Claude
    subscription profile borrows; it has no per-profile ``auth.json``."""
    try:
        from agent.anthropic_credentials import claude_code_credentials_path
        return claude_code_credentials_path()
    except (ImportError, OSError, RuntimeError):
        return None


def _profile_fingerprint(profile: str) -> str:
    home = _profile_home(profile)
    parts = [_stat_part(leaf, home / leaf if home is not None else None) for leaf in ("auth.json", ".env")]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _uses_claude_login(provider: str) -> bool:
    name = (provider or "").lower()
    return "claude" in name or "anthropic" in name


def credential_fingerprint(profiles: list[str], provider: str = "") -> dict[str, str]:
    """Non-secret per-profile hash of the provider's auth identity: ``(mtime_ns, size)`` of each
    profile's ``auth.json`` (credential pool) and ``.env``, plus Claude Code's shared login file for a
    Claude/Anthropic provider (key ``claude_code``). Only ``stat`` is called; no file is read. A
    macOS Keychain login is invisible to ``stat``: ``max_probe_wait_seconds`` bounds that case."""
    out = {profile: _profile_fingerprint(profile) for profile in sorted(set(profiles))}
    if _uses_claude_login(provider):
        out["claude_code"] = hashlib.sha256(_stat_part("claude_code", _claude_login_path()).encode()).hexdigest()[:16]
    return out


def _fingerprint_changed(rec: dict) -> Optional[dict[str, str]]:
    """The fresh fingerprint when a profile known at the last check changed its credentials, else
    ``None``. A profile first seen since then is added without counting as a change."""
    try:
        stored = json.loads(rec.get("fingerprint") or "{}")
    except (TypeError, ValueError):
        stored = {}
    current = credential_fingerprint(rec["profiles"], rec.get("provider") or "")
    changed = any(current.get(p) != h for p, h in stored.items() if p in current)
    rec["fingerprint"] = json.dumps(current, sort_keys=True)
    return current if changed else None


# --------------------------------------------------------------------------- message + reset_at

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_RETRY_AFTER_RE = re.compile(r"retry[\s_-]?after[\"']?\s*[:=]?\s*[\"']?(\d{1,7})(?:\s*(?:s|sec|secs|seconds))?\b", re.IGNORECASE)
_RESET_EPOCH_RE = re.compile(r"reset(?:s|_at|s_at)?[\"']?\s*[:=]\s*[\"']?(\d{10})(?:\.\d+)?\b", re.IGNORECASE)
_RESET_ISO_RE = re.compile(r"reset\w*[^0-9]{0,12}(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(Z|[+-]\d{2}:?\d{2})?", re.IGNORECASE)
_RESET_DATE_RE = re.compile(
    r"reset(?:s|ting)?\s+(?:on\s+|at\s+)?([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?"
    r"(?:,?\s*(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?", re.IGNORECASE)


def _iso_epoch(day: str, hh: str, mm: str, ss: Optional[str], tz: Optional[str]) -> Optional[int]:
    from datetime import UTC, datetime, timedelta, timezone
    try:
        dt = datetime.strptime(f"{day} {hh}:{mm}:{ss or '00'}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if not tz:
        return int(time.mktime(dt.timetuple()))
    if tz.upper() == "Z":
        return int(dt.replace(tzinfo=UTC).timestamp())
    sign = 1 if tz[0] == "+" else -1
    digits = tz[1:].replace(":", "")
    offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0)) * sign
    return int(dt.replace(tzinfo=timezone(offset)).timestamp())


def _date_epoch(match: re.Match, now: int) -> Optional[int]:
    month = _MONTHS.get(match.group(1).lower()[:3])
    if month is None:
        return None
    day, year = int(match.group(2)), match.group(3)
    hour, minute, ampm = match.group(4), match.group(5), (match.group(6) or "").lower()
    h = int(hour) if hour else 0
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    y = int(year) if year else time.localtime(now).tm_year
    try:
        epoch = int(time.mktime((y, month, day, h, int(minute or 0), 0, 0, 0, -1)))
    except (OverflowError, ValueError):
        return None
    if not year and epoch < now - 86400:
        epoch = int(time.mktime((y + 1, month, day, h, int(minute or 0), 0, 0, 0, -1)))
    return epoch


def parse_reset_at(text: Optional[str], now: Optional[int] = None) -> Optional[int]:
    """Epoch the provider says its wall lifts, from ``Retry-After``, a reset epoch/ISO stamp, or
    "resets Nov 17[, 5pm]" (host local time). ``None`` when the text names none."""
    if not text:
        return None
    now = int(time.time()) if now is None else int(now)
    if m := _RETRY_AFTER_RE.search(text):
        return now + int(m.group(1))
    if m := _RESET_EPOCH_RE.search(text):
        return int(m.group(1))
    if m := _RESET_ISO_RE.search(text):
        return _iso_epoch(m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
    if m := _RESET_DATE_RE.search(text):
        return _date_epoch(m, now)
    return None


def redact_message(text: Optional[str]) -> str:
    """The provider's words, secrets redacted, one line, at most ``MESSAGE_MAX_CHARS``."""
    if not text:
        return ""
    from agent.redact import redact_sensitive_text
    clean = " ".join(redact_sensitive_text(str(text), force=True).split())
    return clean[:MESSAGE_MAX_CHARS]


# --------------------------------------------------------------------------- persistence


def _write(conn: sqlite3.Connection):
    from hermes_cli.kanban_db_connect import write_txn
    return write_txn(conn, allow_nested=True)


def _load(conn: sqlite3.Connection, provider: str) -> Optional[dict]:
    try:
        row = conn.execute("SELECT * FROM provider_breakers WHERE provider = ?", (provider,)).fetchone()
    except sqlite3.OperationalError:
        return None  # pre-migration board (read-only health read)
    if row is None:
        return None
    rec = dict(row)
    for key in ("failures", "profiles"):
        try:
            rec[key] = json.loads(rec.get(key) or "[]")
        except (TypeError, ValueError):
            rec[key] = []
    return rec


def _save(conn: sqlite3.Connection, rec: dict, now: int) -> None:
    cols = ("state", "reason", "message", "reset_at", "opened_at", "closed_at", "trip_id", "failures",
            "profiles", "next_probe_at", "probe_task_id", "probe_started_at", "fingerprint")
    values = [json.dumps(rec.get(c) or []) if c in ("failures", "profiles") else rec.get(c) for c in cols]
    conn.execute(
        f"INSERT INTO provider_breakers (provider, {', '.join(cols)}, updated_at) "
        f"VALUES (?, {', '.join('?' for _ in cols)}, ?) ON CONFLICT(provider) DO UPDATE SET "
        + ", ".join(f"{c} = excluded.{c}" for c in (*cols, "updated_at")),
        (rec["provider"], *values, now),
    )


def all_breakers(conn: sqlite3.Connection) -> list[dict]:
    try:
        names = [r[0] for r in conn.execute("SELECT provider FROM provider_breakers ORDER BY provider")]
    except sqlite3.OperationalError:
        return []
    return [rec for name in names if (rec := _load(conn, name)) is not None]


def _next_probe(now: int, reset_at: Optional[int], cfg: BreakerSettings) -> int:
    """After ``probe_seconds``, or at ``reset_at`` when later, but never past ``max_probe_wait_seconds``."""
    wait = min(max(cfg.probe_seconds, int(reset_at or 0) - now), max(cfg.max_probe_wait_seconds, cfg.probe_seconds))
    return now + wait


def _close(rec: dict, now: int) -> None:
    rec.update(state="closed", closed_at=now, failures=[], next_probe_at=None,
               probe_task_id=None, probe_started_at=None)
    log.warning("kanban provider breaker %s CLOSED (trip %s)", rec["provider"], rec.get("trip_id"))


# --------------------------------------------------------------------------- exits


def record_exit(conn: sqlite3.Connection, profile: str, kind: str, *, text: str = "",
                reset_hint: Optional[int] = None, now: Optional[int] = None) -> str:
    """Book one exit 75 (``rate_limited``) / 78 (``terminal_provider``) of ``profile``'s provider
    and return the breaker state after it."""
    now = int(time.time()) if now is None else int(now)
    provider = provider_for_profile(profile)
    cfg = settings()
    with _write(conn):
        rec = _load(conn, provider) or {"provider": provider, "state": "closed", "failures": [], "profiles": []}
        rec["failures"] = [f for f in rec["failures"] if isinstance(f, list) and f and f[0] >= now - cfg.window_seconds]
        rec["failures"].append([now, kind])
        if profile not in rec["profiles"]:
            rec["profiles"] = sorted({*rec["profiles"], profile})
        message = redact_message(text)
        reset_at = parse_reset_at(text, now) or (int(reset_hint) if reset_hint else None)
        tripped = False
        if message:
            rec["message"] = message
        if reset_at and reset_at > now:
            rec["reset_at"] = reset_at
        if rec["state"] == "closed" and len(rec["failures"]) >= cfg.failures:
            tripped = True
            rec.update(state="open", reason=_EXIT_KINDS.get(kind, "quota"), opened_at=now, closed_at=None,
                       trip_id=f"{provider}:{now}:{secrets.token_hex(3)}", reset_at=reset_at if reset_at and reset_at > now else None,
                       fingerprint=json.dumps(credential_fingerprint(rec["profiles"], provider), sort_keys=True),
                       probe_task_id=None, probe_started_at=None)
            rec["next_probe_at"] = _next_probe(now, rec["reset_at"], cfg)
            log.warning("kanban provider breaker %s OPEN (%s, trip %s): %s", provider, rec["reason"],
                        rec["trip_id"], rec.get("message") or "")
        elif rec["state"] == "half_open":
            # The probe (or a run spawned before the trip) hit the wall again: same trip.
            rec.update(state="open", probe_task_id=None, probe_started_at=None,
                       fingerprint=json.dumps(credential_fingerprint(rec["profiles"], provider), sort_keys=True))
            rec["next_probe_at"] = _next_probe(now, rec.get("reset_at"), cfg)
        _save(conn, rec, now)
        if tripped and rec["reason"] == "auth":
            release_auth_parks(conn, provider, since=now - cfg.window_seconds, trip_id=rec["trip_id"], now=now)
    return rec["state"]


def release_auth_parks(conn: sqlite3.Connection, provider: str, *, since: int, trip_id: str, now: int) -> list[str]:
    """Cards a lone exit 78 parked inside the window, before the breaker knew the cause was
    provider-wide: back to ``ready`` with that failure uncounted."""
    rows = conn.execute(
        "SELECT t.id, t.assignee, t.consecutive_failures, e.payload FROM tasks t JOIN task_events e "
        "ON e.id = (SELECT MAX(id) FROM task_events WHERE task_id = t.id) "
        "WHERE t.status = 'blocked' AND e.kind = 'gave_up' AND e.created_at >= ?", (since,)).fetchall()
    from hermes_cli import kanban_db as kb
    released = []
    for row in rows:
        payload = kb._json_dict(row["payload"])
        if not payload.get("terminal_provider") or provider_for_profile(row["assignee"] or "") != provider:
            continue
        conn.execute("UPDATE tasks SET status = 'ready', consecutive_failures = ? WHERE id = ? AND status = 'blocked'",
                     (max(int(row["consecutive_failures"] or 0) - 1, 0), row["id"]))
        kb._append_event(conn, row["id"], "unblocked", {"reason": "provider_breaker", "provider": provider, "trip_id": trip_id})
        released.append(row["id"])
    return released


def _run_failure_text(conn: sqlite3.Connection, task_id: str, run_id: Optional[int], board: Optional[str]) -> tuple[str, Optional[int]]:
    """``(text, reset_hint)`` the worker left for its run: its ``worker_result`` event (managed
    workers) or the tail of its log."""
    from hermes_cli import kanban_db as kb
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND run_id IS ? AND kind = 'worker_result' "
                       "ORDER BY id DESC LIMIT 1", (task_id, run_id)).fetchone()
    payload = kb._json_dict(row["payload"]) if row else {}
    text, hint = str(payload.get("last_output") or ""), payload.get("failure_resets_at")
    if not text:
        from hermes_cli.kanban_db_dispatch import _worker_final_output
        text = _worker_final_output(task_id, board=board, run_id=run_id) or ""
    try:
        hint = int(float(hint)) if hint else None
    except (TypeError, ValueError):
        hint = None
    return text, hint


def observe_dead_worker(conn: sqlite3.Connection, row, dead, *, board: Optional[str] = None):
    """Book a dead worker's 75/78 against its provider; returns the ``_DeadWorker`` to apply.

    An exit 78 while the provider's breaker is not closed is provider-wide: it is re-booked as a
    rate-limited requeue (not counted, card not parked)."""
    from hermes_cli import kanban_db as kb
    kind = "rate_limited" if dead.rate_limited else "terminal_provider" if dead.terminal_provider else None
    if kind is None or not kb._row_get(row, "assignee"):
        return dead
    try:
        text, hint = _run_failure_text(conn, row["id"], kb._row_get(row, "current_run_id"), board)
        if dead.terminal_provider and not text:
            text = str(dead.event_payload.get("worker_output") or "")
        state = record_exit(conn, row["assignee"], kind, text=text, reset_hint=hint)
    except sqlite3.Error:
        log.warning("kanban provider breaker: could not book exit of %s", row["id"], exc_info=True)
        return dead
    if not dead.terminal_provider or state == "closed":
        return dead
    provider = provider_for_profile(row["assignee"])
    payload = {**dead.event_payload, "provider_wide": True, "provider": provider}
    return replace(
        dead, kind="rate_limited", event_kind="rate_limited", event_payload=payload,
        error_text=(f"pid {payload.get('pid')} exited on a provider-wide login failure (exit {dead.code}) while "
                    f"provider {provider} is paused: requeued without counting a failure"),
        rate_limited=True, terminal_provider=False,
    )


def rebook_tripped(conn: sqlite3.Connection, sweep):
    """A 78 booked earlier in the same sweep, before a later exit tripped its provider's breaker,
    is provider-wide too: take it out of the crash accounting so ``_account_crashes`` does not park
    its card (it is already back in its retry status with the failure uncounted)."""
    kept = []
    for detail in sweep.crash_details:
        tid, dead = detail[0], detail[3]
        row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (tid,)).fetchone() if dead.terminal_provider else None
        if row is not None and row["assignee"] and _active(conn, row["assignee"]) is not None:
            sweep.crashed.remove(tid)
            sweep.rate_limited.append(tid)
            continue
        kept.append(detail)
    sweep.crash_details = kept
    return sweep


# --------------------------------------------------------------------------- dispatch gate


def refresh(conn: sqlite3.Connection, now: Optional[int] = None) -> None:
    """Per-tick transitions: OPEN -> HALF_OPEN when the probe is due or the credential fingerprint
    changed; HALF_OPEN -> CLOSED when the probe run survived ``probe_seconds`` or ended without a
    75/78 (a 75/78 re-opened it in :func:`record_exit` first)."""
    now = int(time.time()) if now is None else int(now)
    cfg = None
    for name in [rec["provider"] for rec in all_breakers(conn) if rec["state"] != "closed"]:
        cfg = cfg or settings()
        with _write(conn):
            # Re-read inside the write txn: an operator --reset/--probe from another process
            # since the scan above must not be overwritten by a stale row.
            rec = _load(conn, name)
            if rec is None or rec["state"] == "closed":
                continue
            before = dict(rec)
            if _fingerprint_changed(rec) is not None:
                rec.update(state="half_open", next_probe_at=now)
                log.warning("kanban provider breaker %s HALF_OPEN: credentials changed", name)
            elif rec["state"] == "open" and now >= int(rec.get("next_probe_at") or 0):
                rec["state"] = "half_open"
            if rec["state"] == "half_open" and rec.get("probe_task_id"):
                _settle_probe(conn, rec, now, cfg)
            if rec != before:
                _save(conn, rec, now)


def _settle_probe(conn: sqlite3.Connection, rec: dict, now: int, cfg: BreakerSettings) -> None:
    run = conn.execute(
        "SELECT started_at, ended_at, outcome FROM task_runs WHERE task_id = ? AND started_at >= ? "
        "ORDER BY id DESC LIMIT 1", (rec["probe_task_id"], int(rec.get("probe_started_at") or 0))).fetchone()
    if run is None:
        if now - int(rec.get("probe_started_at") or now) >= cfg.probe_seconds:
            rec.update(probe_task_id=None, probe_started_at=None)  # the spawn never happened: free the probe
        return
    if run["ended_at"] is not None or now - int(run["started_at"] or now) >= cfg.probe_seconds:
        _close(rec, now)


def _active(conn: sqlite3.Connection, profile: str) -> Optional[dict]:
    rec = _load(conn, provider_for_profile(profile))
    return rec if rec is not None and rec["state"] != "closed" else None


def paused(conn: sqlite3.Connection, profile: str, now: Optional[int] = None, *, note: bool = False) -> bool:
    """True when ``profile``'s provider may not spawn now (open, or half-open with its probe
    taken or not yet due). ``note`` records a held-back profile on the breaker so the health
    snapshot lists every profile it pauses; otherwise read-only."""
    rec = _active(conn, profile)
    if rec is None:
        return False
    now = int(time.time()) if now is None else int(now)
    held = _holds(rec, now)
    if held and note and profile not in rec["profiles"]:
        with _write(conn):
            rec = _active(conn, profile)  # fresh row: a concurrent --reset/--probe wins
            if rec is None or not _holds(rec, now):
                return False
            if profile not in rec["profiles"]:
                rec["profiles"] = sorted({*rec["profiles"], profile})
                _save(conn, rec, now)
    return held


def _holds(rec: dict, now: int) -> bool:
    return rec["state"] == "open" or bool(rec.get("probe_task_id")) or now < int(rec.get("next_probe_at") or 0)


def take_probe(conn: sqlite3.Connection, profile: str, task_id: str, now: Optional[int] = None, *,
               dry_run: bool = False) -> bool:
    """Claim the half-open probe slot for ``task_id``; True when it may spawn (closed: always). A dry
    run claims nothing (:func:`paused` already answered for it)."""
    now = int(time.time()) if now is None else int(now)
    if dry_run or _active(conn, profile) is None:
        return True  # closed: no write transaction on the hot path
    with _write(conn):
        rec = _active(conn, profile)
        if rec is None:
            return True
        if rec["state"] != "half_open" or rec.get("probe_task_id") or now < int(rec.get("next_probe_at") or 0):
            return False
        rec.update(probe_task_id=task_id, probe_started_at=now, next_probe_at=now + settings().probe_seconds)
        _save(conn, rec, now)
    log.warning("kanban provider breaker %s probing with %s", rec["provider"], task_id)
    return True


# --------------------------------------------------------------------------- operator


def probe_now(conn: sqlite3.Connection, provider: str, now: Optional[int] = None) -> bool:
    """Operator: half-open ``provider`` now so the next tick spawns one probe; False when it has
    no open breaker."""
    now = int(time.time()) if now is None else int(now)
    with _write(conn):
        rec = _load(conn, provider)
        if rec is None or rec["state"] == "closed":
            return False
        rec.update(state="half_open", next_probe_at=now, probe_task_id=None, probe_started_at=None)
        _save(conn, rec, now)
    log.warning("kanban provider breaker %s HALF_OPEN: operator probe (trip %s)", provider, rec.get("trip_id"))
    return True


def reset_breaker(conn: sqlite3.Connection, provider: str, now: Optional[int] = None) -> bool:
    """Operator: close ``provider``'s breaker (its cards spawn again on the next tick); False when
    it has no open breaker."""
    now = int(time.time()) if now is None else int(now)
    with _write(conn):
        rec = _load(conn, provider)
        if rec is None or rec["state"] == "closed":
            return False
        _close(rec, now)
        _save(conn, rec, now)
    return True
