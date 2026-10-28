"""Post-commit outcomes (contract C3) on the real Windows hand-off script.

A real ``windows.ps1`` drives a real published launcher whose ``hermes``
application is a scripted stand-in: once ``hermes update`` exits 0 the install
is on the new version, so nothing afterwards may report "still on the previous
version", and the hand-off's own supervision (watchdog, probes, marker
release) must not turn healthy work into a failure.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest

from tests.installation_launcher_fixture import publish_fixture_launcher

ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = ROOT / 'scripts/desktop-update/windows.ps1'
CLI = """
import ctypes, json, os, subprocess, sys, time
from pathlib import Path
def main():
    if '--version' in sys.argv:
        print('Install directory: ' + str(Path(__file__).resolve().parents[1])); return 0
    if '--help' in sys.argv:
        if os.environ.get('HANDOFF_HELP_HANG'):
            time.sleep(600)
        print('--keep-stash'); return 0
    with Path(os.environ['HANDOFF_CALLS']).open('a') as stream:
        stream.write(json.dumps(sys.argv[1:]) + '\\n')
    if sys.argv[1:2] != ['update']:
        return 0
    busy = float(os.environ.get('HANDOFF_BUSY_SECONDS', '0'))
    end = time.monotonic() + busy
    while time.monotonic() < end:   # pipe-silent, CPU-busy (download/extract shape)
        pass
    if os.environ.get('HANDOFF_DELEGATE'):
        # Leave a live process running under the claim, named on line 4.
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        Path(os.environ['HANDOFF_DELEGATE']).write_text(str(child.pid))
        # update_lock's delegate line: pid + creation time (GetProcessTimes, limited rights).
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = ctypes.c_void_p(k32.OpenProcess(0x1000, False, child.pid))
        times = [ctypes.c_ulonglong() for _ in range(4)]
        k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times])
        k32.CloseHandle(handle)
        ct = times[0].value / 1e7 - 11644473600
        marker = Path(os.environ['HERMES_HOME']) / '.hermes-update-in-progress'
        head = marker.read_bytes().decode().split('\\n')[:3]
        marker.write_bytes(('\\n'.join(head) + '\\ndelegate:%d ct:%.3f\\n' % (child.pid, ct)).encode())
    if os.environ.get('HANDOFF_HANG'):
        print(os.environ['HANDOFF_HANG'], flush=True)
        time.sleep(300)
    receipt = os.environ.get('HANDOFF_RECEIPT')   # this run's finalized receipt: outcome[,correlation]
    if receipt:
        outcome, correlation = (receipt.split(',') + [''])[:2]
        path = Path(os.environ['HERMES_HOME']) / 'logs' / 'update_receipts' / 'latest.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'correlation_id': correlation or os.environ.get('HERMES_UPDATE_CORRELATION_ID'),
                                    'outcome': outcome, 'finished_at': '2026-10-05T12:00:00+00:00'}, indent=2),
                        encoding='utf-8')
        return int(os.environ.get('HANDOFF_EXIT', '0'))
    print('Update complete!', flush=True)
    return 0
if __name__ == '__main__':
    sys.exit(main())
"""


def _handoff(tmp_path: Path, *args: str, verify: str = 'pass\n', timeout: int = 150, prepare=None, **env: str):
    install = tmp_path / 'checkout'
    publish_fixture_launcher(install, CLI)
    (install / 'hermes_cli/desktop_update_verify.py').write_text(verify, encoding='utf-8')
    home = tmp_path / 'home'; home.mkdir()
    if prepare is not None:
        prepare(home, install)
    calls = tmp_path / 'calls.jsonl'
    proc = subprocess.Popen(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT),
         '-InstallRoot', str(install), '-NoUi', *args],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, 'HERMES_HOME': str(home), 'HERMES_RUNTIME_DIR': str(tmp_path / 'empty-store'),
             'HANDOFF_CALLS': str(calls), 'PYTHONIOENCODING': 'utf-8', **env},
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True, check=False)
        out, _ = proc.communicate()
        pytest.fail(f'hand-off did not finish within {timeout}s: {out}')
    argv = [json.loads(line) for line in calls.read_text(encoding='utf-8-sig').splitlines()] if calls.exists() else []
    result_path = home / '.hermes-update-result.json'
    result = json.loads(result_path.read_text(encoding='utf-8-sig')) if result_path.is_file() else None
    return proc.returncode, out, argv, result, home


@pytest.mark.platforms('windows')
def test_verify_failure_after_a_committed_update_is_ok_with_warnings(tmp_path: Path) -> None:
    code, out, argv, result, _ = _handoff(tmp_path, verify='raise SystemExit(3)\n')
    assert code == 0, out
    assert (result['ok'], result['exit_code'], result['manual']) == (True, 0, True), result
    assert [w.split(':')[0] for w in result['warnings']] == ['verify'], result
    assert result['message'].startswith('Hermes was updated, but'), result
    assert 'previous version' not in result['message']
    # The fleet the Desktop stopped is still brought back.
    assert argv[-1] == ['gateway', 'start', '--all'], argv


@pytest.mark.platforms('windows')
def test_result_is_atomic_json_carrying_started_at_and_warnings(tmp_path: Path) -> None:
    started = int(time.time()) - 5
    code, out, _, result, home = _handoff(tmp_path, HERMES_UPDATE_STARTED_AT=str(started))
    assert code == 0, out
    assert (result['ok'], result['started_at'], result['warnings']) == (True, started, []), result
    assert not list(home.glob('.hermes-update-result.json.*.tmp'))


@pytest.mark.platforms('windows')
def test_a_result_that_cannot_be_published_leaves_no_tmp_file(tmp_path: Path) -> None:
    # Write-Result's Move/Replace fail (the result path is a directory): its <result>.<pid>.tmp
    # must not be left beside it, one per hand-off, forever.
    code, out, _, result, home = _handoff(
        tmp_path, '-NoGateway', prepare=lambda home, _install: (home / '.hermes-update-result.json').mkdir())
    assert code == 0, out
    assert result is None
    assert not list(home.glob('.hermes-update-result.json.*.tmp')), list(home.iterdir())


@pytest.mark.platforms('windows')
@pytest.mark.parametrize(('receipt', 'ok'), [('interrupted', True), ('partial', True), ('interrupted,another-run', False),
                                             ('failed', False)])
def test_nonzero_exit_after_the_commit_point_is_installed_with_a_followup(tmp_path: Path, receipt: str, ok: bool) -> None:
    # Contract C3: `hermes update` exits 130 / 1 AFTER its commit point (Ctrl-C after the code
    # moved, a parked autostash). Its own receipt (matched by the correlation id) says so: the
    # result must report the update installed with an owed follow-up, never "update failed".
    code, out, argv, result, _ = _handoff(tmp_path, HANDOFF_RECEIPT=receipt, HANDOFF_EXIT='130')
    if ok:
        assert code == 0, out
        assert (result['ok'], result['exit_code'], result['manual']) == (True, 0, True), result
        assert result['message'].startswith('Hermes was updated, but'), result
        assert [w.split(':')[0] for w in result['warnings']] == ['update'], result
        assert argv[-1] == ['gateway', 'start', '--all'], argv   # the post-commit steps still run
    else:
        assert code == 130, out
        assert (result['ok'], result['exit_code']) == (False, 130), result


_CHECKOUT_HOLDER = """
import json, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0), 0o644)
os.lseek(fd, 1 << 20, os.SEEK_SET)   # hermes_cli/update_lock.py::_try_lock's byte
if os.name == 'nt':
    import msvcrt
    def lock(): msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    def unlock(): msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:   # native POSIX protocol tests, never a substitute for the Windows E2E
    import fcntl
    def lock(): fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1)
    def unlock(): fcntl.lockf(fd, fcntl.LOCK_UN, 1)
try:
    lock()
    print('locked', flush=True)
    command = sys.stdin.readline()
    # EOF is unconditional cleanup, not a successful release handshake.
    if command:
        assert command == 'release\\n', repr(command)
        released = time.time()   # lower bound, immediately before the actual unlock
        unlock()
        print(json.dumps({'released': released}), flush=True)
finally:
    os.close(fd)
"""


class _CheckoutHolder:
    """Hold a real lock until *observed* R6; no startup-duration guesses.

    stdout acknowledges acquisition/unlock; stdin EOF releases on every failure.
    The reader drains the pipe independently, including during bounded teardown.
    """

    def __init__(self, lock: Path, home: Path, timeout: float = 150):
        self.lock, self.home = lock, home
        self.deadline = time.monotonic() + timeout
        self.stop = threading.Event()
        self.messages = queue.Queue()
        self.released = self.initial_finished_at = None
        self.error = None
        self.proc = None
        self.reader = self.observer = None

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        assert remaining > 0, 'checkout-holder protocol deadline expired'
        return remaining

    def _read(self):
        for line in self.proc.stdout:
            self.messages.put(line.rstrip('\n'))
        self.messages.put('<EOF>')

    def _message(self):
        while not self.stop.is_set():
            try:
                return self.messages.get(timeout=min(0.05, self._remaining()))
            except queue.Empty:
                continue
        raise AssertionError('checkout-holder protocol cancelled')

    def __enter__(self):
        self.proc = subprocess.Popen(
            [sys.executable, '-u', '-c', _CHECKOUT_HOLDER, str(self.lock)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            ready = self._message()
            assert ready == 'locked', f'checkout lock was not acquired: {ready}'
            self.observer = threading.Thread(target=self._observe, daemon=True)
            self.observer.start()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _observe(self):
        try:
            while not self.stop.is_set():
                assert time.monotonic() < self.deadline, 'R6 wait was not observed'
                assert self.proc.poll() is None, 'checkout holder exited before R6'
                try:
                    log = (self.home / 'logs/desktop-update-handoff.log').read_text(encoding='utf-8-sig')
                    if 'keeping the update marker' in log:
                        initial = json.loads((self.home / '.hermes-update-result.json').read_text(encoding='utf-8-sig'))
                        assert (self.home / '.hermes-update-in-progress').is_file(), 'R6 lost its marker'
                        self.initial_finished_at = initial['finished_at']
                        break
                except (FileNotFoundError, PermissionError):
                    pass   # Add-Content temporarily denies read sharing on Windows.
                self.stop.wait(min(0.05, max(0, self.deadline - time.monotonic())))
            else:
                return
            # The result clock is whole seconds. Cross its next boundary explicitly,
            # so removing post-R6 restamping cannot accidentally pass in the same second.
            while time.time() < self.initial_finished_at + 1:
                if self.stop.wait(min(0.05, self._remaining())):
                    return
            if self.stop.is_set():
                return
            self.proc.stdin.write('release\n')
            self.proc.stdin.flush()
            self.released = json.loads(self._message())['released']
            assert self.released >= self.initial_finished_at + 1
        except BaseException as exc:
            self.error = exc
        finally:
            self.proc.stdin.close()   # EOF also unlocks after observer failure.

    def finish(self):
        self.observer.join(timeout=max(0, self.deadline - time.monotonic()))
        assert not self.observer.is_alive(), 'R6 wait was not observed'
        if self.error is not None:
            raise self.error
        assert self.released is not None, 'R6 release was not acknowledged'

    def __exit__(self, *_):
        self.stop.set()
        if self.observer is not None:
            self.observer.join(timeout=5)
        # Closing stdin wakes the child even when no log/ack ever arrived.
        if not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.reader.join(timeout=5)
        self.proc.stdout.close()
        assert not self.reader.is_alive(), 'checkout-holder reader leaked'
        assert self.observer is None or not self.observer.is_alive(), 'R6 observer leaked'


@pytest.mark.platforms('windows')
def test_result_finished_at_is_stamped_after_the_r6_release_wait(tmp_path: Path) -> None:
    # The relaunched Desktop drops a non-manual result whose finished_at is 30 minutes old; the
    # R6 wait lasts up to 2 h. The result must carry the time the hand-off actually finished.
    with ExitStack() as cleanup:
        holders = []

        def hold_checkout(home: Path, install: Path) -> None:
            holders.append(cleanup.enter_context(_CheckoutHolder(install / '.hermes-update.lock', home)))

        code, out, _, result, home = _handoff(tmp_path, '-NoGateway', prepare=hold_checkout)
        holders[0].finish()
    assert code == 0, out
    log = (home / 'logs/desktop-update-handoff.log').read_text(encoding='utf-8-sig')
    assert 'keeping the update marker' in log, log
    assert result['ok'] is True and result['finished_at'] >= int(holders[0].released), result
    assert result['finished_at'] > holders[0].initial_finished_at, result
    assert 'removed update marker (owned)' in log, log
    assert not (home / '.hermes-update-in-progress').exists()
    assert not list(home.glob('.hermes-update-result.json.*.tmp'))


@pytest.mark.platforms('windows')
def test_marker_with_a_live_delegate_is_handed_to_it_at_finish(tmp_path: Path) -> None:
    # A7 release rule: the owner deletes its claim unless a delegate still runs; then that
    # delegate becomes the owner (canonical body, started_at kept, no delegate line).
    pid_file = tmp_path / 'delegate.pid'
    try:
        code, out, _, _result, home = _handoff(tmp_path, HANDOFF_DELEGATE=str(pid_file))
        assert code == 0, out
        marker = (home / '.hermes-update-in-progress').read_bytes().decode().split('\n')
        assert marker[0] == pid_file.read_text(encoding='utf-8-sig'), marker
        assert marker[2].startswith('ct:') and marker[3:] == [''], marker
    finally:
        if pid_file.exists():
            subprocess.run(['taskkill', '/F', '/PID', pid_file.read_text(encoding='utf-8-sig')], capture_output=True, check=False)


@pytest.mark.platforms('windows')
def test_watchdog_remap_after_banner_reports_interrupted_followups(tmp_path: Path) -> None:
    code, out, _argv, result, _ = _handoff(
        tmp_path, '-NoGateway', HANDOFF_HANG='Update complete! (v1.0.0)',
        HERMES_UPDATE_STEP_IDLE_SECONDS='3',
    )
    assert code == 0, out
    assert (result['ok'], result['manual']) == (True, True), result
    assert any('interrupted' in w for w in result['warnings']), result


@pytest.mark.platforms('windows')
def test_cpu_busy_pipe_silent_update_is_not_killed_by_the_idle_watchdog(tmp_path: Path) -> None:
    code, out, _argv, result, _ = _handoff(
        tmp_path, '-NoGateway', HANDOFF_BUSY_SECONDS='15', HERMES_UPDATE_STEP_IDLE_SECONDS='4',
    )
    assert code == 0, out
    assert (result['ok'], result['warnings']) == (True, []), result


@pytest.mark.platforms('windows')
def test_hanging_update_help_probe_is_bounded(tmp_path: Path) -> None:
    code, out, argv, _result, _ = _handoff(
        tmp_path, '-NoGateway', '-ProbeTimeoutSeconds', '10', timeout=120, HANDOFF_HELP_HANG='1',
    )
    assert code == 0, out
    assert argv[0][:2] == ['update', '--yes'] and '--keep-stash' not in argv[0], argv
