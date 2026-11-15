"""A macOS gateway notices when a managed-Python install replaces its own interpreter file.

PM publishes a store entry by renaming the live tree to ``.previous-<entry>``, publishing the
new tree under the old name and deleting the ``.previous`` tree. A gateway running across that
keeps executing the moved/unlinked file, and macOS TCC then refuses every AppleEvent it sends
(-1743, no prompt) until the process restarts. Live on Nov 14 2026: gateway pid 96981 ran from a
deleted ``tools/.previous-python-3.14.9…/bin/python3.14`` and every Notes call failed.

Restarting by itself is opt-in (``gateway.restart_on_interpreter_swap``), waits for idle so it
never drains live turns or card workers, and is capped at one per cooldown window.
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

from gateway import interpreter_swap_restart as isr


class _Runner:
    def __init__(self, work=()):
        self._running = True
        self._restart_requested = False
        self._draining = False
        self.restart_kwargs = None
        self._work = list(work)  # successive _active_work_count() answers; then 0

    def request_restart(self, **kwargs):
        self.restart_kwargs = kwargs
        return True

    def _active_work_count(self):
        return self._work.pop(0) if self._work else 0


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


def _watch(boot, stamp, *, per_poll=(), runner=None, platform="darwin", update=False, polls=4,
           enabled=True, now=10_000.0):
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
        platform=platform, max_polls=polls, enabled=lambda: enabled, stamp=stamp,
        clock=lambda: now))
    return restarts


def test_a_republished_interpreter_restarts_once_after_two_reads(tmp_path):
    executable = _store_entry(tmp_path)
    boot = isr.interpreter_identity(str(executable))
    assert boot is not None and boot[0].endswith("python3.14")  # the real file, not the symlink
    runner = _Runner()
    restarts = _watch(boot, tmp_path / "stamp.json", per_poll=[lambda: _republish(tmp_path)],
                      runner=runner)
    assert restarts == [runner]
    assert "now holds a different file" in (isr.swap_reason(boot, update_in_progress=False) or "")


def test_a_deleted_interpreter_restarts(tmp_path):
    """A version bump plus ``pm gc`` removes the whole entry the gateway booted on."""
    import shutil

    executable = _store_entry(tmp_path)
    boot = isr.interpreter_identity(str(executable))
    assert boot is not None
    restarts = _watch(boot, tmp_path / "stamp.json",
                      per_poll=[lambda: shutil.rmtree(tmp_path / "python-3.14.9")])
    assert len(restarts) == 1
    assert "is gone" in (isr.swap_reason(boot, update_in_progress=False) or "")


def test_uncertain_or_settled_reads_keep_the_gateway_up(tmp_path):
    executable = _store_entry(tmp_path)
    real = tmp_path / "python-3.14.9" / "bin" / "python3.14"
    aside = tmp_path / "aside"
    stamp = tmp_path / "stamp.json"
    boot = isr.interpreter_identity(str(executable))
    assert boot is not None
    # Unchanged file: never.
    assert _watch(boot, stamp) == []
    # A single blip (the file is away for one read, then the same inode is back): never.
    blip = [lambda: os.rename(real, aside), lambda: os.rename(aside, real)]
    assert _watch(boot, stamp, per_poll=blip + [None, None]) == []
    # During an update the swap is not over yet; off macOS no Automation identity is at stake.
    _republish(tmp_path)
    assert _watch(boot, stamp, update=True) == []
    assert _watch(boot, stamp, platform="linux") == []
    # A restart or stop already under way boots the current interpreter on its own.
    busy = _Runner()
    busy._restart_requested = True
    assert _watch(boot, stamp, runner=busy) == []
    draining = _Runner()
    draining._draining = True
    assert _watch(boot, stamp, runner=draining) == []
    assert not stamp.exists()
    # The real watcher restarts on the same disk state once nothing holds it back.
    assert len(_watch(boot, stamp)) == 1


def test_off_by_default_it_only_warns(tmp_path, caplog):
    """With the flag off (the default) a confirmed swap logs that a restart is needed, nothing more."""
    executable = _store_entry(tmp_path)
    boot = isr.interpreter_identity(str(executable))
    assert boot is not None
    _republish(tmp_path)
    runner = _Runner()
    with caplog.at_level(logging.WARNING, logger=isr.__name__):
        assert _watch(boot, tmp_path / "stamp.json", runner=runner, enabled=False) == []
    assert runner.restart_kwargs is None and not runner._draining
    assert not (tmp_path / "stamp.json").exists()
    assert any("Restart needed" in r.getMessage() and isr.CONFIG_KEY in r.getMessage()
               for r in caplog.records)


def test_the_restart_waits_for_idle_and_never_drains_live_work(tmp_path):
    """Active turns or card workers hold the restart; it fires only once the count reaches zero."""
    executable = _store_entry(tmp_path)
    boot = isr.interpreter_identity(str(executable))
    assert boot is not None
    _republish(tmp_path)
    # Work never ends within the poll budget: no restart, no drain, no stamp.
    always_busy = _Runner(work=[1] * 50)
    assert _watch(boot, tmp_path / "stamp.json", runner=always_busy, polls=6) == []
    assert not always_busy._draining and always_busy.restart_kwargs is None
    assert not (tmp_path / "stamp.json").exists()
    # Work that finishes: the restart follows, after the counts drop to zero.
    finishing = _Runner(work=[2, 1])
    assert _watch(boot, tmp_path / "stamp.json", runner=finishing, polls=6) == [finishing]
    assert finishing._work == []  # both busy answers were consumed before the restart


def test_one_automatic_restart_per_cooldown_window(tmp_path):
    """A reinstall that keeps republishing cannot restart-loop the gateway."""
    stamp = tmp_path / "stamp.json"
    executable = _store_entry(tmp_path)
    boot = isr.interpreter_identity(str(executable))
    assert boot is not None
    _republish(tmp_path)
    assert len(_watch(boot, stamp, now=10_000.0)) == 1
    assert isr.cooldown_remaining(stamp, now=10_000.0) == isr.COOLDOWN_S
    # The next boot sees another swap within the hour: held.
    assert _watch(boot, stamp, now=10_000.0 + isr.COOLDOWN_S - 1) == []
    # After the window it may restart again.
    assert len(_watch(boot, stamp, now=10_000.0 + isr.COOLDOWN_S + 1)) == 1
    # A stamp from the future (clock moved back) holds; an unreadable one does not block forever.
    assert isr.cooldown_remaining(stamp, now=0.0) == isr.COOLDOWN_S
    stamp.write_text("not json", encoding="utf-8")
    assert isr.cooldown_remaining(stamp, now=0.0) == 0.0


def test_the_flag_is_read_from_the_gateway_config():
    """``gateway.restart_on_interpreter_swap`` in config.yaml, through the gateway's loader."""
    from hermes_constants import get_hermes_home
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["gateway"][isr.CONFIG_KEY] is False
    config = get_hermes_home() / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    assert isr.restart_enabled() is False  # unset: off
    config.write_text(f"gateway:\n  {isr.CONFIG_KEY}: true\n", encoding="utf-8")
    assert isr.restart_enabled() is True
    config.write_text(f"gateway:\n  {isr.CONFIG_KEY}: false\n", encoding="utf-8")
    assert isr.restart_enabled() is False


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
