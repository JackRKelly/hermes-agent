"""A7 on the real Windows hand-off script: every marker mutation happens inside one
hold of the kernel lock on ``<marker>.lock`` and the update child is the marker's
delegate before it runs a single instruction.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys
import time
import uuid

import psutil

import pytest

from tests.installation_launcher_fixture import publish_fixture_launcher
from tests.scripts.desktop_update.windows_handoff_support import (
    MARKER,
    HOLD_CLI,
    _creation_time,
    _ProcessWatch,
    _dead_pid,
    _script,
    _finish,
    _op,
    _HeldLock,
    _custodian,
)


@pytest.fixture
def sleeper():
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
    yield proc
    proc.kill()
    proc.wait()


# -- A7 ---------------------------------------------------------------------

@pytest.mark.platforms('windows')
def test_a7_claim_is_decided_inside_the_marker_lock(tmp_path: Path, sleeper: subprocess.Popen) -> None:
    """While another process holds <marker>.lock the claimant must not touch a dead marker;
    once the holder (which replaced it with a live claim meanwhile) lets go, it judges THAT."""
    dead = f'{_dead_pid()}\n{int(time.time())}\nct:5.000\n'.encode()
    marker = tmp_path / MARKER
    marker.write_bytes(dead)
    lock = _HeldLock(tmp_path)
    try:
        claimant = _script(tmp_path, '-SelfTestMarker', '-NoMarkerCleanup')
        time.sleep(5)   # PowerShell start-up plus a few lock retries
        assert claimant.poll() is None, 'claimant did not wait for the marker lock'
        assert marker.read_bytes() == dead, 'a dead marker was mutated outside the marker lock'
        live = f'{sleeper.pid}\n{int(time.time())}\nct:{_creation_time(sleeper.pid)}\n'.encode()
        marker.write_bytes(live)   # the lock holder's own decision: a new live claim
    finally:
        lock.release()
    code, out = _finish(claimant)
    assert code == 2, out
    assert marker.read_bytes() == live


@pytest.mark.platforms('windows')
def test_a7_concurrent_claimants_over_a_dead_marker_yield_one_owner(tmp_path: Path) -> None:
    install = tmp_path / 'checkout'
    publish_fixture_launcher(install, HOLD_CLI)
    home = tmp_path / 'home'; home.mkdir()
    marker = home / MARKER
    marker.write_bytes(f'{_dead_pid()}\n{int(time.time())}\nct:5.000\n'.encode())
    hold = tmp_path / 'release-update'
    lock = _HeldLock(home)   # line every claimant up behind the lock, then let them race
    claimants = [_script(home, install=install, HANDOFF_HOLD=str(hold)) for _ in range(4)]
    try:
        time.sleep(6)
        lock.release()
        deadline = time.monotonic() + 120
        while not Path(str(hold) + '.pid').exists():
            assert time.monotonic() < deadline, 'no claimant reached the update'
            time.sleep(0.05)
        while sum(c.poll() is not None for c in claimants) < 3:
            assert time.monotonic() < deadline, [c.poll() for c in claimants]
            time.sleep(0.1)
        refused = [c for c in claimants if c.poll() == 2]
        running = [c for c in claimants if c.poll() is None]
        assert len(running) == 1 and len(refused) == 3, [c.returncode for c in claimants]
        assert marker.read_bytes().decode().split('\n')[0] == _custodian(home, running[0].pid)
    finally:
        hold.touch()
        for c in claimants:
            if c.poll() is None:
                try:
                    c.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    subprocess.run(['taskkill', '/T', '/F', '/PID', str(c.pid)], capture_output=True, check=False)
    assert running[0].returncode == 0
    assert not marker.exists()


# -- the update child is the delegate before it runs anything ------------------

def test_child_observer_uses_live_identity_not_initialized_command_line() -> None:
    """A newborn/suspended process need not expose any update argv to an observer."""
    parent = psutil.Process()
    executable = parent.exe()
    before = {(p.pid, p.create_time()) for p in parent.children()}
    child = subprocess.Popen([executable, '-c', 'import time; time.sleep(60)'])
    try:
        found = _wait_for_update_child(parent.pid, before, executable, timeout=5)
        assert found == (child.pid, psutil.Process(child.pid).create_time())
        assert '--yes' not in psutil.Process(child.pid).cmdline()
    finally:
        child.kill()
        child.wait()


@pytest.mark.platforms('windows')
def test_child_observer_identifies_real_suspended_child(tmp_path: Path) -> None:
    ran = tmp_path / 'resumed'
    before = {(p.pid, p.create_time()) for p in psutil.Process().children()}
    child = subprocess.Popen([
        sys.executable, '-c', 'import pathlib, sys; pathlib.Path(sys.argv[1]).touch()', str(ran),
    ], creationflags=0x00000004)   # CREATE_SUSPENDED, as used by StartAssigned
    try:
        assert _wait_for_update_child(os.getpid(), before, sys.executable, timeout=5) == (
            child.pid, psutil.Process(child.pid).create_time(),
        )
        assert not ran.exists(), 'observer required the suspended child to execute'
    finally:
        child.kill()
        child.wait(timeout=5)
    assert not ran.exists()


@pytest.mark.parametrize('exclude', ['existing-identity', 'wrong-image'])
def test_child_observer_rejects_non_update_process(exclude: str) -> None:
    image = psutil.Process().exe()
    child = subprocess.Popen([image, '-c', 'import time; time.sleep(60)'])
    try:
        identity = (child.pid, psutil.Process(child.pid).create_time())
        before = {identity} if exclude == 'existing-identity' else set()
        executable = image if before else image + '.not-the-interpreter'
        with pytest.raises(AssertionError, match='no new update child'):
            _wait_for_update_child(os.getpid(), before, executable, timeout=0)
    finally:
        child.kill()
        child.wait()


def test_child_observer_rejects_descendants(tmp_path: Path) -> None:
    ready = tmp_path / 'grandchild-ready'
    worker = subprocess.Popen([
        psutil.Process().exe(), '-c',
        'import pathlib, subprocess, sys; '
        'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); '
        'target = pathlib.Path(sys.argv[1]); staged = target.with_suffix(".tmp"); '
        'staged.write_text(str(p.pid)); staged.replace(target); '
        'sys.stdin.read(); p.kill(); p.wait()', str(ready),
    ], stdin=subprocess.PIPE)
    descendant = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert time.monotonic() < deadline and worker.poll() is None
            time.sleep(0.02)
        descendant = psutil.Process(int(ready.read_text()))
        assert descendant.ppid() == worker.pid
        before = {(worker.pid, psutil.Process(worker.pid).create_time())}
        with pytest.raises(AssertionError, match='no new update child'):
            _wait_for_update_child(os.getpid(), before, psutil.Process().exe(), timeout=0)
    finally:
        worker.communicate(timeout=5)
        assert descendant is None or not descendant.is_running()


def test_published_fixture_runtime_command_uses_its_own_import_closure(tmp_path: Path) -> None:
    """Query and execute the real launcher without borrowing an installed PM package."""
    install = tmp_path / 'checkout'
    launcher = publish_fixture_launcher(install, '''
import json
import sys
import pm.environments
import pm.paths

def main():
    print(json.dumps({
        'environments': pm.environments.__file__, 'paths': pm.paths.__file__,
        'args': sys.argv[1:], 'python': sys.executable,
    }))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
''')
    env = {key: value for key, value in os.environ.items()
           if key not in ('HERMES_RUNTIME_DIR', 'HERMES_INSTALL_ROOT')}
    env['HERMES_HOME'] = str(tmp_path / 'home')
    query = subprocess.run(
        [str(launcher), '--print-runtime-command', '--module', 'hermes_cli.main'],
        cwd=install, env=env, capture_output=True, text=True, check=True,
    )
    runtime = json.loads(query.stdout)
    assert os.path.samefile(runtime[0], sys.executable)
    assert runtime[1:3] == ['-I', '-c']
    result = subprocess.run(
        [*runtime, '--closure-probe'], cwd=install, env=env,
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    assert Path(data['environments']).resolve() == install / 'pm' / 'environments.py'
    assert Path(data['paths']).resolve() == install / 'pm' / 'paths.py'
    assert data['args'] == ['--closure-probe']
    assert os.path.samefile(data['python'], sys.executable)


@pytest.mark.parametrize('relay', [False, True])
def test_help_probe_handshake_gates_real_launcher(tmp_path: Path, relay: bool) -> None:
    install = tmp_path / 'checkout'
    launcher = publish_fixture_launcher(install, _GATED_HOLD_CLI)
    hold = tmp_path / 'release-update'
    nonce = uuid.uuid4().hex
    runtime = json.loads(subprocess.check_output(
        [str(launcher), '--print-runtime-command', '--module', 'hermes_cli.main'],
        cwd=install, text=True,
    ))
    command = [*runtime, '--help']
    if relay:
        command = [sys.executable, '-c',
                   'import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))', *command]
    child = subprocess.Popen(
        command, cwd=install,
        env={**os.environ, 'HANDOFF_HOLD': str(hold), 'HANDOFF_NONCE': nonce},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        ready = _wait_for_help_probe(hold, nonce, child, os.getpid(), timeout=5)
        assert ready['root_pid'] == child.pid
        assert ready['created'] == psutil.Process(ready['pid']).create_time()
        assert ready['root_created'] == psutil.Process(child.pid).create_time()
        assert child.poll() is None
        assert not Path(str(hold) + '.pid').exists()
        Path(str(hold) + '.help-release').touch()
        out, err = child.communicate(timeout=5)
        assert child.returncode == 0, err
        assert out.strip() == '--keep-stash'
    finally:
        Path(str(hold) + '.help-release').touch()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            for descendant in psutil.Process(child.pid).children(recursive=True):
                descendant.kill()
            child.kill()
            child.wait()


@pytest.mark.parametrize('field,value,message', [
    ('nonce', 'another-run', 'foreign help readiness'),
    ('parent', -1, 'help has wrong parent'),
    ('created', 0.0, 'help has wrong creation identity'),
])
def test_help_probe_rejects_foreign_identity(tmp_path: Path, field, value, message) -> None:
    hold = tmp_path / 'release-update'
    proc = psutil.Process()
    data = {'nonce': 'this-run', 'pid': proc.pid, 'parent': proc.ppid(),
            'executable': proc.exe(), 'created': proc.create_time()}
    data[field] = value
    Path(str(hold) + '.help-ready').write_text(json.dumps(data), encoding='utf-8')
    # The ready file already exists, so no Popen is needed for this identity check.
    with pytest.raises(AssertionError, match=message):
        _wait_for_help_probe(hold, 'this-run', None, proc.ppid(), timeout=0)


_GATED_HOLD_CLI = HOLD_CLI.replace(
    "        print('--keep-stash'); return 0",
    """        import json, psutil
        hold = Path(os.environ['HANDOFF_HOLD'])
        ready = Path(str(hold) + '.help-ready')
        staged = Path(str(ready) + '.tmp')
        staged.write_text(json.dumps({
            'nonce': os.environ['HANDOFF_NONCE'], 'pid': os.getpid(),
            'parent': os.getppid(), 'executable': psutil.Process().exe(),
            'created': psutil.Process().create_time(),
        }), encoding='utf-8')
        os.replace(staged, ready)
        while not Path(str(hold) + '.help-release').exists():
            time.sleep(0.02)
        print('--keep-stash'); return 0""",
)


def _wait_for_help_probe(
    hold: Path, nonce: str, script: subprocess.Popen | None, parent_pid: int, *, timeout: float = 60,
) -> dict:
    ready = Path(str(hold) + '.help-ready')
    deadline = time.monotonic() + timeout
    while not ready.exists():
        assert script is not None, 'no process supplied while awaiting help readiness'
        assert script.poll() is None, f'hand-off exited before help readiness: {script.communicate()}'
        assert time.monotonic() < deadline, 'final help probe never became ready'
        time.sleep(0.02)
    data = json.loads(ready.read_text(encoding='utf-8'))
    assert data['nonce'] == nonce, f'foreign help readiness: {data}'
    probe = psutil.Process(data['pid'])
    assert data['parent'] == probe.ppid(), f'help has wrong parent: {data}'
    assert os.path.samefile(data['executable'], probe.exe()), f'help has wrong interpreter: {data}'
    assert data['created'] == probe.create_time(), f'help has wrong creation identity: {data}'
    # A Windows venv redirector may be the direct child and execute Python in
    # its own child. Validate that real ancestry for readiness only; the update
    # observer still accepts *only* a new direct child of the hand-off.
    root = probe
    while root.ppid() != parent_pid:
        ancestor = root.parent()
        assert ancestor is not None, f'help is not descended from {parent_pid}: {data}'
        root = ancestor
    data.update(root_pid=root.pid, root_created=root.create_time(), root_executable=root.exe())
    return data


def _wait_for_update_child(
    parent_pid: int, before: set[tuple[int, float]], executable: str, *, timeout: float = 60,
) -> tuple[int, float]:
    """Use native process metadata, not CIM's possibly uninitialized CommandLine.

    The gated final help probe is in ``before``. Once released, the next *new*
    direct child with that interpreter is the suspended update. The custodian,
    probe, unrelated processes and descendants cannot satisfy this boundary.
    """
    parent = psutil.Process(parent_pid)
    deadline = time.monotonic() + timeout
    expected = os.path.normcase(os.path.realpath(executable))
    while True:
        seen = []
        for child in parent.children(recursive=False):
            try:
                identity = (child.pid, child.create_time())
                image = os.path.normcase(os.path.realpath(child.exe()))
                seen.append((identity, image))
                if identity not in before and image == expected:
                    return identity
            except psutil.NoSuchProcess:
                continue   # a probe exiting while the snapshot is inspected
        assert time.monotonic() < deadline, f'no new update child of {parent_pid}: {seen=}, {before=}'
        time.sleep(0.02)


@pytest.mark.platforms('windows')
def test_script_killed_before_publishing_the_delegate_runs_no_update(tmp_path: Path) -> None:
    """Kill cell at the pre-publication boundary: windows.ps1 has created its `hermes update`
    child but has not published it as the marker delegate (the marker lock is held elsewhere).
    Killed there, no update instruction may have run, and the custodian line 1 names releases
    the marker (nothing holds the checkout)."""
    install = tmp_path / 'checkout'
    publish_fixture_launcher(install, _GATED_HOLD_CLI)
    home = tmp_path / 'home'; home.mkdir()
    hold = tmp_path / 'release-update'
    ran = Path(str(hold) + '.pid')
    marker = home / MARKER
    nonce = uuid.uuid4().hex
    script = _script(home, install=install, HANDOFF_HOLD=str(hold), HANDOFF_NONCE=nonce)
    lock = None
    try:
        # The last launcher probe holds the hand-off *after* expensive startup
        # but *before* StartAssigned. Acquire the marker lock at that exact
        # boundary instead of racing startup from the first custodian log line.
        ready = _wait_for_help_probe(hold, nonce, script, script.pid)
        assert marker.read_bytes().split(b'\n')[0] == _custodian(home, script.pid).encode()
        before = {(p.pid, p.create_time()) for p in psutil.Process(script.pid).children()}
        assert (ready['root_pid'], ready['root_created']) in before
        lock = _HeldLock(home)
        Path(str(hold) + '.help-release').touch()
        child_pid, child_created = _wait_for_update_child(script.pid, before, ready['root_executable'])
        watch = _ProcessWatch(child_pid)
        try:
            # Open the kernel watch before trusting the observed identity. Its
            # handle pins this PID until close, preventing a reuse during kill.
            child = psutil.Process(child_pid)
            assert child.create_time() == child_created
            assert child.ppid() == script.pid
            assert not ran.exists(), 'the update child ran before delegate publication'
            assert not any(line.startswith('delegate:') for line in marker.read_text().splitlines())
            subprocess.run(['taskkill', '/F', '/PID', str(script.pid)], capture_output=True, check=True)
            script.wait(timeout=30)
            # The script's death closes the child's job; kill-on-close then terminates the child
            # asynchronously, so await its exit. Once it is gone it can never write `ran`.
            assert watch.exited_within(60), 'the never-resumed update child outlived its script'
        finally:
            watch.close()
        assert not ran.exists(), 'the update child ran before it was published as the delegate'
        lines = marker.read_bytes().decode().split('\n')
        assert lines[0] == _custodian(home, script.pid), lines
        assert not any(line.startswith('delegate:') for line in lines), lines
    finally:
        if lock:
            lock.release()
        hold.touch()
        if script.poll() is None:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(script.pid)], capture_output=True, check=False)
            script.wait()
    deadline = time.monotonic() + 60   # the custodian releases the marker once the lock is free
    while (verdict := _op(home, '-MarkerOp', 'reclaim')[1]).startswith('live '):
        assert time.monotonic() < deadline
        time.sleep(0.2)
    assert verdict in ('absent\n', 'reclaimed\n')
