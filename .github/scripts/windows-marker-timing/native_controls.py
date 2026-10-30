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


def inaccessible_control(pids, open_process, close_handle, last_error, query, report):
    """Select only an enumerated PID with real limited-query access denied.

    Callbacks allow portable classifier tests, never native evidence substitution.
    Every granted query-only HANDLE is closed before continuing the read-only scan.
    """
    for pid in pids:
        if pid <= 0:
            continue  # Idle PID 0 is an invalid-parameter control, not access denied.
        handle = open_process(pid)
        if handle:
            close_handle(handle)
            if pid == 4:
                report('control_info', control='inaccessible-pid', owner_pid=pid,
                       reason='PID 4 grants limited access; scanning other enumerated PIDs')
            continue
        native_error = last_error()  # Capture before any other native calls/reporting.
        report('denied_scan_query', owner_pid=pid, native_winerror=native_error)
        if native_error != 5:
            continue  # A disappearing PID or other failure is not the precondition.
        try:
            query(pid)
        except OSError as error:
            candidate_error = getattr(error, 'winerror', None)
            assert candidate_error == 5, ('wrong inaccessible error', pid, error)
        else:
            raise AssertionError('inaccessible pid returned fabricated identity')
        report('control_pass', control='inaccessible-pid', owner_pid=pid,
               native_winerror=native_error, candidate_winerror=candidate_error)
        return True
    report('control_blocked', control='inaccessible-pid',
           reason='No enumerated process denied PROCESS_QUERY_LIMITED_INFORMATION with WinError 5')
    return False


class OwnedThreadAPI:
    """Prebind real native APIs before restricting only this owned process's thread.

    The explicit loader/error/exit seams support portable signature tests only.
    Native main supplies ctypes.WinDLL/get_last_error and unbuffered os._exit.
    """
    def __init__(self, loader, last_error, failfast):
        from ctypes import wintypes as w
        k = loader('kernel32', use_last_error=True)
        a = loader('advapi32', use_last_error=True)
        for function, arguments, result in (
            (k.GetCurrentThread, (), w.HANDLE),
            (k.OpenProcess, (w.DWORD, w.BOOL, w.DWORD), w.HANDLE),
            (k.CloseHandle, (w.HANDLE,), w.BOOL),
            (a.OpenThreadToken, (w.HANDLE, w.DWORD, w.BOOL, ctypes.POINTER(w.HANDLE)), w.BOOL),
            (a.ImpersonateAnonymousToken, (w.HANDLE,), w.BOOL),
            (a.RevertToSelf, (), w.BOOL),
        ):
            function.argtypes, function.restype = arguments, result
        self.thread = k.GetCurrentThread()
        self.open_token = a.OpenThreadToken
        self.anonymous = a.ImpersonateAnonymousToken
        self.revert = a.RevertToSelf
        self.open_process, self.close_handle = k.OpenProcess, k.CloseHandle
        self.last_error, self.failfast = last_error, failfast
        self.token = w.HANDLE()

    def close(self, handle):
        if not self.close_handle(handle):
            raise ctypes.WinError(self.last_error())

    def no_token(self):
        if not self.open_token(self.thread, 0x0008, True, ctypes.byref(self.token)):
            return self.last_error()  # ERROR_NO_TOKEN (1008) alone qualifies.
        self.close(self.token)  # Do not clear or replace a preexisting token.
        return 0

    def enter(self):
        if not self.anonymous(self.thread):
            return False, self.last_error()
        return True, 0

    def probe(self, pid):
        handle = self.open_process(0x1000, False, pid)
        if not handle:
            return False, self.last_error()  # Capture immediately, before any other API.
        self.close(handle)
        return True, 0


def prepare_owned_source(repo):
    """Load the immutable archived final module without replacing the CIM oracle.

    Hash and AST inspection happen BEFORE impersonation; no transformed helper,
    timing wrapper, monkeypatch or detached candidate is allowed for this control.
    """
    import ast
    import hashlib
    import importlib.util
    from native_no_shell import require_source, source_receipt

    path = repo.resolve() / 'tests/scripts/desktop_update/test_desktop_update_windows_marker.py'
    raw = path.read_bytes()
    require_source('patched-source', dict(helper_source_sha256=hashlib.sha256(raw).hexdigest()))
    spec = importlib.util.spec_from_file_location('marker_owned_denial_source', path)
    source = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source)
    receipt = source_receipt(source, repo)
    require_source('patched-source', receipt)
    function = next(node for node in ast.parse(raw).body
                    if isinstance(node, ast.FunctionDef) and node.name == '_creation_time')
    branches = [node for node in function.body if isinstance(node, ast.If) and
                ast.unparse(node.test) == 'not handle']
    assert len(branches) == 1 and len(branches[0].body) == 1, 'wrong failed OpenProcess branch'
    failure = branches[0].body[0]
    assert isinstance(failure, ast.Raise) and ast.unparse(failure.exc) == (
        'ctypes.WinError(ctypes.get_last_error())'), 'wrong failed OpenProcess branch'
    receipt['denied_raise_line'] = failure.lineno
    return source._creation_time, failure.lineno, receipt


def owned_denial_control(pid, query, denied_line, live, api, report, receipt):
    """Owned-thread orchestration; injected APIs in portable tests are NOT evidence.

    No reporting, child polling, source inspection or process/thread creation occurs
    between enter and revert. The unwrapped, warmed actual source helper is queried.
    """
    assert api.no_token() == 1008, 'current thread already has a token'
    assert live(), 'owned child exited before baseline'
    identity = query(pid)
    observed_error = None
    pending = None
    native_granted, native_error = False, None
    entered, entry_error = False, None
    try:
        entered, entry_error = api.enter()
        if entered:
            native_granted, native_error = api.probe(pid)
            if not native_granted and native_error == 5:
                try:
                    query(pid)
                except OSError as error:
                    observed_error = error
    except BaseException as error:
        pending = error  # Keep in memory; recover before propagating to any reporter.
    finally:
        try:
            reverted = api.revert()
        except BaseException:
            reverted = False
        if not reverted:
            api.failfast(86)  # Native binding is os._exit: no flushing or cleanup.
            os._exit(86)  # Defensive fallback if an invalid binding returns.
    try:
        recovered_token = api.no_token() == 1008
    except BaseException:
        recovered_token = False
    if not recovered_token:
        api.failfast(86)
        os._exit(86)
    assert live(), 'owned child exited during denial'
    identity_after = query(pid)
    assert identity_after == identity, 'owned child identity changed after revert'
    assert live(), 'owned child exited during recovery'
    if pending is not None:
        raise pending
    if not entered or native_granted or native_error != 5:
        report('control_blocked', control='inaccessible-pid', owner_pid=pid,
               fixture='owned-child-current-thread-anonymous', entry_winerror=entry_error,
               native_granted=native_granted, native_winerror=native_error, recovered=True,
               reason='Anonymous owned-child limited-query denial unavailable', **receipt)
        return False
    assert observed_error is not None, 'denied helper returned an identity'
    assert getattr(observed_error, 'winerror', None) == 5, 'wrong denied helper error'
    frame = observed_error.__traceback__
    assert frame is not None, 'missing failed OpenProcess branch traceback'
    while frame.tb_next is not None:
        frame = frame.tb_next
    assert (frame.tb_frame.f_code is query.__code__ and frame.tb_lineno == denied_line and
            'handle' in frame.tb_frame.f_locals and not frame.tb_frame.f_locals['handle'] and
            frame.tb_frame.f_locals.get('pid') == pid), 'not the actual failed OpenProcess branch'
    report('control_pass', control='inaccessible-pid', owner_pid=pid,
           fixture='owned-child-current-thread-anonymous', native_winerror=native_error,
           candidate_winerror=getattr(observed_error, 'winerror', None), recovered=True,
           identity_before=identity, identity_after=identity_after,
           thread_token_before=1008, thread_token_after=1008, query_access=0x1000,
           failure_path=frame.tb_frame.f_code.co_filename, failure_line=frame.tb_lineno,
           failure_function=frame.tb_frame.f_code.co_name, **receipt)
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
    close_check()  # A blocked denied-process control must not truncate handle measurements.
    report('controls_complete', status='candidate-equality-only-not-acceptance',
           blocked_controls=[] if inaccessible_available else ['inaccessible-pid'])
    return 0 if inaccessible_available else 3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--denial-repo', type=Path)
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
        assert args.denial_repo is not None, 'archived final source required for owned denial'
        denied_query, denied_line, denied_receipt = prepare_owned_source(args.denial_repo)
        owned_api = OwnedThreadAPI(ctypes.WinDLL, ctypes.get_last_error, os._exit)
        owned_child = children[0]  # Retained live Popen HANDLE; never a foreign PID.
        accessible_control = owned_denial_control(
            owned_child.pid, denied_query, denied_line, lambda: owned_child.poll() is None,
            owned_api, emit, denied_receipt)
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
