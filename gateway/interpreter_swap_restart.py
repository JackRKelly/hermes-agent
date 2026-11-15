"""Restart a macOS gateway whose own interpreter binary was replaced underneath it.

PM publishes a managed Python by renaming the live store entry to ``.previous-<entry>``,
publishing the new tree under the old name and deleting the ``.previous`` tree
(``pm.install._publish_entry``). A version bump and a later ``pm gc`` remove the old entry
the same way. A gateway that was running then keeps executing an unlinked or moved file.
macOS TCC can no longer build the responsible process for it ("failed to construct a process
for the responsible audit token"), so every AppleEvent its tools send (Notes, Reminders,
Terminal) fails with -1743 and no consent prompt can show, until the gateway restarts.

The Automation grant itself survives the swap: every managed Python is signed with the stable
identifier in ``hermes_cli.macos_signing``, and the grant's code requirement is that
identifier. The only missing piece is the restart, so the gateway watches its own
interpreter. It records the real file's ``(st_dev, st_ino)`` at boot. When that path is gone,
or holds a different file, on two consecutive reads while no update owns the install, it logs
why. Every uncertain read keeps it up.

Restarting on its own is opt-in: ``gateway.restart_on_interpreter_swap`` (default false). Off,
the gateway logs one WARNING that a restart is needed and keeps running. On, it first waits
until no work is active (``_active_work_count() == 0``: turns, card workers, cron, API runs),
so it never opens a drain that refuses messages or interrupts a card worker, then asks for the
same restart ``/restart`` uses (exit 75 under launchd or systemd, the detached helper
otherwise). A persisted stamp allows at most one such restart per ``COOLDOWN_S``, so a
reinstall that keeps republishing the interpreter cannot loop the gateway.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

POLL_S = 60.0
_CONFIRMATIONS = 2
COOLDOWN_S = 3600.0
CONFIG_KEY = "restart_on_interpreter_swap"  # under ``gateway:``
_STAMP_NAME = "gateway_interpreter_swap_restart.json"

FileIdentity = tuple[str, int, int]


def interpreter_identity(executable: Optional[str] = None) -> Optional[FileIdentity]:
    """``(real path, st_dev, st_ino)`` of the interpreter file, or None when it can't be read."""
    path = executable if executable is not None else sys.executable
    if not path:
        return None
    try:
        real = os.path.realpath(path)
        st = os.stat(real)
    except OSError:
        return None
    return real, st.st_dev, st.st_ino


def swap_reason(boot: FileIdentity, *, update_in_progress: bool) -> Optional[str]:
    """The log line when *boot*'s interpreter file is no longer the one at its path, else None."""
    if update_in_progress:
        return None
    path, dev, ino = boot
    try:
        st = os.stat(path)
    except FileNotFoundError:
        state = "is gone"
    except OSError:
        return None  # unreadable proves nothing
    else:
        if (st.st_dev, st.st_ino) == (dev, ino):
            return None
        state = "now holds a different file"
    return (f"This gateway's Python interpreter {path} {state} (a managed-Python install replaced "
            "it). macOS cannot attribute AppleEvents to a process running a replaced binary, so "
            "Notes and other Automation calls fail until the gateway restarts.")


def relaunch_module_argv(executable: Optional[str] = None) -> list[str]:
    """``python -m hermes_cli.main`` on the running interpreter while its file still exists.

    A version bump plus ``pm gc`` deletes the file ``sys.executable`` names; the install-bound
    launch command then names the interpreter the install records now, so a relaunch never
    execs a missing file.
    """
    path = executable if executable is not None else sys.executable
    if path and os.path.exists(path):
        return [path, "-m", "hermes_cli.main"]
    from hermes_cli._launchers import runtime_command

    return runtime_command(Path(__file__).resolve().parent.parent)


def resolve_hermes_bin() -> Optional[list[str]]:
    """Hermes update/restart argv: this install's ``python -m hermes_cli.main``
    (:func:`relaunch_module_argv`), else ``hermes`` on PATH, else None. The module argv must
    win: a PATH-first lookup lets an attacker-planted ``hermes`` shadow the running install
    when /update or /restart re-execs it (#111569)."""
    try:
        import importlib.util
        if importlib.util.find_spec("hermes_cli") is not None:
            return relaunch_module_argv()
    except Exception:  # health: allow BLE001 -- moved from gateway.run; PATH is the fallback
        logger.debug("module relaunch argv unavailable; falling back to PATH", exc_info=True)
    import shutil
    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        return [hermes_bin]
    return None


def _request_restart(runner) -> None:
    """The ``/restart`` route: exit 75 under a restarting supervisor, else the detached helper."""
    from gateway.restart import is_container_restart_context, is_gateway_supervisor_process

    via_service = is_gateway_supervisor_process() or is_container_restart_context()
    runner.request_restart(detached=not via_service, via_service=via_service)


def watcher_applies(platform: Optional[str] = None) -> bool:
    """Only macOS ties an Automation identity to the running binary."""
    if platform is None:
        from hermes_platform.host.facts import os_family

        platform = os_family()
    return platform == "darwin"


def restart_enabled() -> bool:
    """``gateway.restart_on_interpreter_swap`` from the gateway's effective config (default off)."""
    from gateway.run import _load_gateway_config
    from utils import is_truthy_value

    gateway_cfg = _load_gateway_config().get("gateway")
    raw = gateway_cfg.get(CONFIG_KEY) if isinstance(gateway_cfg, dict) else None
    return is_truthy_value(raw, default=False)


def _stamp_path() -> Path:
    from hermes_constants import get_process_hermes_home

    return get_process_hermes_home() / _STAMP_NAME  # one gateway process, one stamp


def cooldown_remaining(path: Path, *, now: float, cooldown_s: float = COOLDOWN_S) -> float:
    """Seconds before another automatic restart is allowed; 0 when none is recorded."""
    try:
        last = float(json.loads(path.read_text(encoding="utf-8-sig"))["restarted_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0
    if last > now:
        return cooldown_s  # a stamp from the future (clock moved back) holds the full window
    return max(0.0, last + cooldown_s - now)


def record_restart(path: Path, *, now: float) -> None:
    from utils import atomic_json_write

    atomic_json_write(path, {"restarted_at": now})


def _restart_held(runner) -> bool:
    return bool(getattr(runner, "_restart_requested", False) or getattr(runner, "_draining", False))


def _active_work(runner) -> int:
    try:
        return int(runner._active_work_count())
    except Exception:  # health: allow BLE001 -- an unreadable count must hold the restart, not fire it
        return 1


async def _confirmed_swap(runner, read: Callable[[], Optional[str]], poll_s: float,
                          next_poll: Callable[[], bool]) -> Optional[str]:
    """The swap reason once ``_CONFIRMATIONS`` consecutive reads agree, else None (stopped)."""
    seen = 0
    while getattr(runner, "_running", False) and next_poll():
        reason = await asyncio.to_thread(read)
        seen = seen + 1 if reason else 0
        if seen >= _CONFIRMATIONS:
            return reason
        await asyncio.sleep(poll_s)
    return None


async def interpreter_swap_watcher(runner, *, poll_s: float = POLL_S, boot: Optional[FileIdentity] = None,
                                   update_probe: Optional[Callable[[], bool]] = None,
                                   restart: Callable[[object], None] = _request_restart,
                                   platform: Optional[str] = None,
                                   max_polls: Optional[int] = None,
                                   enabled: Callable[[], bool] = restart_enabled,
                                   stamp: Optional[Path] = None,
                                   clock: Callable[[], float] = time.time) -> None:
    """Supervised gateway watcher: on a confirmed swap, warn; opted in, wait for idle and request
    one restart, at most once per ``COOLDOWN_S``."""
    if not watcher_applies(platform):
        return
    boot = boot if boot is not None else interpreter_identity()
    if boot is None:
        logger.debug("interpreter swap watcher: interpreter file unreadable at boot; not watching")
        return
    if update_probe is None:
        from hermes_cli.update_lock import update_in_progress as update_probe
    observe_update: Callable[[], bool] = update_probe

    def _read() -> Optional[str]:
        try:
            busy = observe_update()
        except Exception:  # health: allow BLE001 -- an unreadable update lock cannot prove the swap is over
            busy = True
        return swap_reason(boot, update_in_progress=busy)

    budget = [max_polls]

    def _next_poll() -> bool:
        if budget[0] is None:
            return True
        budget[0] -= 1
        return budget[0] >= 0

    reason = await _confirmed_swap(runner, _read, poll_s, _next_poll)
    if reason is None or _restart_held(runner):
        return  # stopped, or a restart/stop already under way boots the current interpreter
    if not enabled():
        logger.warning("%s Restart needed; set gateway.%s: true to let the gateway restart itself "
                       "when idle.", reason, CONFIG_KEY)
        return
    stamp = stamp if stamp is not None else _stamp_path()
    left = cooldown_remaining(stamp, now=clock())
    if left > 0:
        logger.warning("%s Not restarting: an automatic restart for this ran within the last %.0fs "
                       "(%.0fs left); restart the gateway by hand.", reason, COOLDOWN_S, left)
        return
    logger.warning("%s Restarting once no work is active.", reason)
    while _active_work(runner) > 0:
        if not getattr(runner, "_running", False) or _restart_held(runner) or not _next_poll():
            return
        await asyncio.sleep(poll_s)
    if not getattr(runner, "_running", False) or _restart_held(runner):
        return
    record_restart(stamp, now=clock())
    restart(runner)
