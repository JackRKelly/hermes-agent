"""Real-native candidate/original equality controls. Not acceptance; no mocked OS/API."""
import argparse
import ctypes
import os
from pathlib import Path
import subprocess
import sys

from helper_candidate import _creation_time as candidate
from marker_timing_plugin import emit, timed


def require_dead_query(query, pid):
    """Only the post-GetProcessTimes exited branch proves this held-PID control."""
    try:
        query(pid)
    except OSError as error:
        if type(error) is not ProcessLookupError or 'has exited' not in str(error):
            raise AssertionError(('wrong held-dead exception', type(error).__name__, str(error))) from error
    else:
        raise AssertionError(('exited pid accepted', pid))


def inaccessible_control(open_process, close_handle, last_error, query, report):
    handle = open_process()
    if handle:
        close_handle(handle)
        report('control_blocked', control='inaccessible-pid', owner_pid=4,
               reason='PID 4 grants limited access; inaccessible control unavailable')
        return False
    assert last_error() == 5, 'PID 4 control must fail with access denied, not missing pid'
    try:
        query(4)
    except OSError as error:
        assert error.winerror == 5, error
    else:
        raise AssertionError('inaccessible pid returned fabricated identity')
    report('control_pass', control='inaccessible-pid', owner_pid=4)
    return True


def check_handle_closure(query, live_pid, identity, held_dead, handles, report):
    assert [code for _, code in held_dead] == [0, 259], 'both held-dead controls required'
    for child, code in held_dead:
        assert child.returncode == code, ('wrong actual child exit', code, child.returncode)
    before = handles()
    for _ in range(64):
        assert query(live_pid) == identity
    for child, code in held_dead:
        for _ in range(64):
            require_dead_query(query, child.pid)
    after = handles()
    assert after == before, ('kernel handle leak', before, after)
    report('control_pass', control='close-handle', before=before, after=after,
           success_queries=64, dead_queries_by_exit_code={'0': 64, '259': 64})


def finish_controls(inaccessible_available, close_check, report):
    close_check()  # A blocked PID 4 control must not truncate the handle measurements.
    report('controls_complete', status='candidate-equality-only-not-acceptance',
           blocked_controls=[] if inaccessible_available else ['inaccessible-pid'])
    return 0 if inaccessible_available else 3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    os.environ['MARKER_TIMING_DIR'] = str(args.out.resolve())
    if sys.flags.optimize or not __debug__:
        emit('blocked', reason='assertion controls disabled by optimization')
        return 2
    if sys.platform != 'win32':
        emit('blocked', reason='real native Windows required')
        return 2
    sys.path.insert(0, str(args.repo.resolve()))
    from tests.scripts.desktop_update import test_desktop_update_windows_marker as original
    import hashlib
    source_hash = hashlib.sha256(Path(original.__file__).read_bytes()).hexdigest()
    assert source_hash == '2cb44bc565731abd75eea33376eb250065ce97471aee7b5acf83274145e6c36f', (
        'equality oracle must be the unchanged CIM source, not the candidate', source_hash)
    cim = timed(original._creation_time, '_creation_time')
    kernel = timed(candidate, '_creation_time')
    emit('controls_start', repo=str(args.repo.resolve()))
    children = []
    try:
        for label in ('sleep-child', 'second-child-no-pid-cache'):
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
            children.append(child)
            expected = cim(child.pid)
            actual = kernel(child.pid)
            assert actual == expected, (label, child.pid, expected, actual)
            assert actual != '1000.000', 'mismatched/reused-identity sentinel must differ'
            emit('control_pass', control=label, owner_pid=child.pid, identity=actual)
        protected = original._protected_pid()  # Original full-denied / limited-granted selector.
        expected = cim(protected)
        actual = kernel(protected)
        assert actual == expected, ('protected', protected, expected, actual)
        emit('control_pass', control='protected-limited-rights', owner_pid=protected, identity=actual)
        for invalid in (0, -1, 0x100000000, True):
            try:
                candidate(invalid)
            except ValueError:
                pass
            else:
                raise AssertionError(('invalid pid accepted', invalid))
        emit('control_pass', control='invalid-pids')
        held_dead = []
        for code in (0, 259):
            dead = subprocess.Popen([sys.executable, '-c', f'import sys; sys.exit({code})'])
            children.append(dead)  # Retain both real Popen HANDLEs through all measurements.
            held_dead.append((dead, code))
            assert dead.wait(timeout=60) == code, ('wrong actual child exit', code, dead.returncode)
            # Keep Popen's Windows handle alive: querying a terminated process can still open it.
            require_dead_query(candidate, dead.pid)
            emit('control_pass', control='dead-pid', owner_pid=dead.pid, exit_code=code)
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        def close_control_handle(handle):
            if not k32.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        accessible_control = inaccessible_control(
            lambda: k32.OpenProcess(0x1000, False, 4), close_control_handle,
            ctypes.get_last_error, candidate, emit)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetProcessHandleCount.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.GetProcessHandleCount.restype = wintypes.BOOL
        def handles():
            count = wintypes.DWORD()
            if not k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(count)):
                raise ctypes.WinError(ctypes.get_last_error())
            return count.value
        return finish_controls(accessible_control,
                               lambda: check_handle_closure(candidate, protected, actual, held_dead, handles, emit),
                               emit)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=60)


if __name__ == '__main__':
    raise SystemExit(main())
