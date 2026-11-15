"""A macOS gateway restarts once a managed-Python install replaces its own interpreter file.

PM publishes a store entry by renaming the live tree to ``.previous-<entry>``, publishing the
new tree under the old name and deleting the ``.previous`` tree. A gateway running across that
keeps executing the moved/unlinked file, and macOS TCC then refuses every AppleEvent it sends
(-1743, no prompt) until the process restarts. Live on Nov 14 2026: gateway pid 96981 ran from a
deleted ``tools/.previous-python-3.14.9…/bin/python3.14`` and every Notes call failed.
"""
import asyncio
import os
import sys
from pathlib import Path

from gateway import interpreter_swap_restart as isr


class _Runner:
    def __init__(self):
        self._running = True
        self._restart_requested = False
        self._draining = False
        self.restart_kwargs = None

    def request_restart(self, **kwargs):
        self.restart_kwargs = kwargs
        return True


def _store_entry(root: Path, name: str = "python-3.14.9") -> Path:
    """A store entry whose ``bin/python3`` is a symlink to the real ``python3.14``, like PM's."""
    bindir = root / name / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python3.14").write_bytes(b"interpreter")
    (bindir / "python3").symlink_to("python3.14")
    return bindir / "python3"


def _republish(root: Path, name: str = "python-3.14.9") -> None:
    """``pm.install._publish_entry`` + ``_discard_entry``: same path, new tree, old tree gone."""
    previous = root / f".previous-{name}"
    os.rename(root / name, previous)
    _store_entry(root, name)
    (previous / "bin" / "python3").unlink()
    (previous / "bin" / "python3.14").unlink()


def _watch(boot, *, per_poll=(), runner=None, platform="darwin", update=False, polls=4):
    """Run the watcher; ``per_poll[i]`` mutates the disk just before read ``i``."""
    runner = runner or _Runner()
    restarts, steps = [], iter(per_poll)

    def probe():
        step = next(steps, None)
        if step is not None:
            step()
        return update

    asyncio.run(isr.interpreter_swap_watcher(
        runner, poll_s=0, boot=boot, update_probe=probe, restart=restarts.append,
        platform=platform, max_polls=polls))
    return restarts


def test_a_republished_interpreter_restarts_once_after_two_reads(tmp_path):
    executable = _store_entry(tmp_path)
    boot = isr.interpreter_identity(str(executable))
    assert boot is not None and boot[0].endswith("python3.14")  # the real file, not the symlink
    runner = _Runner()
    restarts = _watch(boot, per_poll=[lambda: _republish(tmp_path)], runner=runner)
    assert restarts == [runner]
    assert "now holds a different file" in (isr.swap_reason(boot, update_in_progress=False) or "")


def test_a_deleted_interpreter_restarts(tmp_path):
    """A version bump plus ``pm gc`` removes the whole entry the gateway booted on."""
    import shutil

    executable = _store_entry(tmp_path)
    boot = isr.interpreter_identity(str(executable))
    assert boot is not None
    restarts = _watch(boot, per_poll=[lambda: shutil.rmtree(tmp_path / "python-3.14.9")])
    assert len(restarts) == 1
    assert "is gone" in (isr.swap_reason(boot, update_in_progress=False) or "")


def test_uncertain_or_settled_reads_keep_the_gateway_up(tmp_path):
    executable = _store_entry(tmp_path)
    real = tmp_path / "python-3.14.9" / "bin" / "python3.14"
    aside = tmp_path / "aside"
    boot = isr.interpreter_identity(str(executable))
    assert boot is not None
    # Unchanged file: never.
    assert _watch(boot) == []
    # A single blip (the file is away for one read, then the same inode is back): never.
    blip = [lambda: os.rename(real, aside), lambda: os.rename(aside, real)]
    assert _watch(boot, per_poll=blip + [None, None]) == []
    # During an update the swap is not over yet; off macOS no Automation identity is at stake.
    _republish(tmp_path)
    assert _watch(boot, update=True) == []
    assert _watch(boot, platform="linux") == []
    # A restart or stop already under way boots the current interpreter on its own.
    busy = _Runner()
    busy._restart_requested = True
    assert _watch(boot, runner=busy) == []
    draining = _Runner()
    draining._draining = True
    assert _watch(boot, runner=draining) == []
    # The real watcher restarts on the same disk state once nothing holds it back.
    assert len(_watch(boot)) == 1


def test_the_restart_takes_the_slash_restart_route(monkeypatch):
    """Under launchd the gateway exits 75 for the supervisor; unsupervised it uses the helper."""
    import gateway.restart as restart_mod

    monkeypatch.setattr(restart_mod, "is_container_restart_context", lambda: False)
    monkeypatch.setattr(restart_mod, "is_gateway_supervisor_process", lambda: True)
    runner = _Runner()
    isr._request_restart(runner)
    assert runner.restart_kwargs == {"detached": False, "via_service": True}
    monkeypatch.setattr(restart_mod, "is_gateway_supervisor_process", lambda: False)
    isr._request_restart(runner)
    assert runner.restart_kwargs == {"detached": True, "via_service": False}


def test_the_relaunch_argv_never_names_a_missing_interpreter(tmp_path):
    present = tmp_path / "python3"
    present.write_bytes(b"x")
    assert isr.relaunch_module_argv(str(present)) == [str(present), "-m", "hermes_cli.main"]
    missing = str(tmp_path / ".previous-python-3.14.9" / "bin" / "python3")
    argv = isr.relaunch_module_argv(missing)
    assert argv[0] != missing and os.path.exists(argv[0])
    assert argv[1:3] == ["-I", "-c"] and "hermes_bootstrap" in argv[3]
    assert argv[4:] == []  # callers append the subcommand after the bootstrap
    assert isr.relaunch_module_argv() == [sys.executable, "-m", "hermes_cli.main"]
    from gateway.run import _resolve_hermes_bin  # /restart and /update read it through the facade
    assert _resolve_hermes_bin() == [sys.executable, "-m", "hermes_cli.main"]
