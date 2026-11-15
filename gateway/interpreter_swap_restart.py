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
why and asks for the same drain-first restart ``/restart`` uses. That is exit 75 under
launchd or systemd and the detached helper otherwise. Every uncertain read keeps it up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Callable, Optional

logger = logging.getLogger(__name__)

POLL_S = 60.0
_CONFIRMATIONS = 2

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
            "Notes and other Automation calls fail until restart; restarting on the current "
            "interpreter.")


def relaunch_module_argv(executable: Optional[str] = None) -> list[str]:
    """``python -m hermes_cli.main`` on the running interpreter while its file still exists.

    A version bump plus ``pm gc`` deletes the file ``sys.executable`` names; the install-bound
    launch command then names the interpreter the install records now, so a relaunch never
    execs a missing file.
    """
    path = executable if executable is not None else sys.executable
    if path and os.path.exists(path):
        return [path, "-m", "hermes_cli.main"]
    from pathlib import Path

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


async def interpreter_swap_watcher(runner, *, poll_s: float = POLL_S, boot: Optional[FileIdentity] = None,
                                   update_probe: Optional[Callable[[], bool]] = None,
                                   restart: Callable[[object], None] = _request_restart,
                                   platform: Optional[str] = None,
                                   max_polls: Optional[int] = None) -> None:
    """Supervised gateway watcher: one drain-first restart after ``_CONFIRMATIONS`` swapped reads."""
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

    seen = polls = 0
    while getattr(runner, "_running", False) and (max_polls is None or polls < max_polls):
        polls += 1
        reason = await asyncio.to_thread(_read)
        seen = seen + 1 if reason else 0
        if seen >= _CONFIRMATIONS:
            if getattr(runner, "_restart_requested", False) or getattr(runner, "_draining", False):
                return  # a restart or stop is already under way; it boots the current interpreter
            logger.warning("%s", reason)
            restart(runner)
            return
        await asyncio.sleep(poll_s)
