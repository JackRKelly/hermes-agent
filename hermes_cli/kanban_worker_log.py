"""Per-task worker log rotation, split out of ``hermes_cli.kanban_db_dispatch``."""

from __future__ import annotations

import contextlib
from pathlib import Path
import re
from typing import Optional

DEFAULT_LOG_ROTATE_BYTES = 2 * 1024 * 1024   # 2 MiB
DEFAULT_LOG_BACKUP_COUNT = 1

# The log is append-mode across runs, so the dispatcher stamps each spawn's start into it: the
# dead-worker sweep then books a run from what THAT run wrote, never a prior run's exit trailer
# and output (a worker killed before its first write would otherwise inherit both).
WORKER_START_MARKER = "[kanban-worker-start] run="
_START_RE = re.compile(r"^" + re.escape(WORKER_START_MARKER) + r"(\d+)[ \t\r]*$", re.MULTILINE)


def write_run_start_marker(log_f, run_id: Optional[int]) -> None:
    """Stamp ``run_id``'s start into the binary append-mode log the worker is about to inherit."""
    if run_id is None:
        return
    log_f.write(f"\n{WORKER_START_MARKER}{int(run_id)}\n".encode("utf-8"))
    log_f.flush()


def run_scoped_log_tail(raw: Optional[str], run_id: Optional[int]) -> str:
    """The part of a worker-log tail that run ``run_id`` wrote ("" when it wrote nothing).

    Text after the last start marker, and only when that marker is ``run_id``'s: a later or
    foreign marker means nothing in the tail belongs to the run being booked. A tail with no
    marker is a pre-marker log, or one run's output long enough to push its own marker out of
    the window: either way it is read whole, as before. ``run_id=None`` takes the last run.
    """
    if not raw:
        return ""
    starts = list(_START_RE.finditer(raw))
    if not starts:
        return raw
    last = starts[-1]
    if run_id is not None and int(last.group(1)) != int(run_id):
        return ""
    return raw[last.end():]


def read_run_log_tail(task_id: str, *, board: Optional[str], run_id: Optional[int],
                      tail_bytes: int = 4000) -> str:
    """``run_scoped_log_tail`` of the task's worker log; "" (never raises) when unreadable."""
    from hermes_cli import kanban_db as kb
    try:
        raw = kb.read_worker_log(task_id, tail_bytes=tail_bytes, board=board)
    except Exception:
        return ""
    return run_scoped_log_tail(raw, run_id)


def worker_log_rotation_config(kanban_cfg: Optional[dict] = None) -> tuple[int, int]:
    """Return ``(rotate_bytes, backup_count)`` for worker log rotation.
    Defaults: rotate at 2 MiB, keep one backup (``.log.1``); both overridable
    from ``config.yaml``.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = (load_config().get("kanban") or {})
        except Exception:
            kanban_cfg = {}
    kanban_cfg = kanban_cfg or {}
    from hermes_cli.kanban_db_dispatch import _positive_int
    max_bytes = _positive_int(kanban_cfg.get("worker_log_rotate_bytes"), DEFAULT_LOG_ROTATE_BYTES, minimum=1)
    backup_count = _positive_int(kanban_cfg.get("worker_log_backup_count"), DEFAULT_LOG_BACKUP_COUNT, minimum=0)
    return max_bytes, backup_count


def _rotated_log_path(log_path: Path, generation: int) -> Path:
    return log_path.with_suffix(log_path.suffix + f".{generation}")


def rotate_worker_log(
    log_path: Path,
    max_bytes: int,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Rotate ``<log>`` when it exceeds ``max_bytes``: ``<log>`` → ``<log>.1``,
    older generations shift up to ``backup_count``.
    """
    try:
        if not log_path.exists() or log_path.stat().st_size <= max_bytes:
            return
        from hermes_cli.kanban_db_dispatch import _positive_int
        backup_count = _positive_int(backup_count, DEFAULT_LOG_BACKUP_COUNT, minimum=0)
        if backup_count == 0:
            log_path.unlink()
            return
        oldest = _rotated_log_path(log_path, backup_count)
        with contextlib.suppress(OSError):
            if oldest.exists():
                oldest.unlink()
        for generation in range(backup_count - 1, 0, -1):
            src = _rotated_log_path(log_path, generation)
            if not src.exists():
                continue
            with contextlib.suppress(OSError):
                src.rename(_rotated_log_path(log_path, generation + 1))
        log_path.rename(_rotated_log_path(log_path, 1))
    except OSError:
        pass
