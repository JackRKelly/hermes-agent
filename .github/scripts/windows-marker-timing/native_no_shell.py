"""Native RED/GREEN for removal of helper shell launch, NOT the 300s gate."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import sys
import time
from marker_timing_plugin import emit


SOURCE_HASHES = {
    'original': '2cb44bc565731abd75eea33376eb250065ce97471aee7b5acf83274145e6c36f',
    'patched-source': '9d19bbd5320e6dcdafa2b6fdd96c54401848e72c1e92ee5459ace3a18e972331',
}


def source_receipt(module, repo):
    path = Path(module.__file__).resolve()
    expected = repo.resolve() / 'tests/scripts/desktop_update/test_desktop_update_windows_marker.py'
    helper = module._creation_time
    assert path == expected, ('wrong imported source path', path, expected)
    assert helper.__module__ == module.__name__, 'helper is an alias from another module'
    assert Path(helper.__code__.co_filename).resolve() == path, 'helper code from another source'
    return dict(helper_source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                helper_module=module.__name__, helper_path=str(path))


def require_source(implementation, receipt):
    assert implementation in SOURCE_HASHES, 'isolated candidate is not a patched source fixture'
    assert receipt['helper_source_sha256'] == SOURCE_HASHES[implementation], (
        'wrong actual imported source SHA256', implementation, receipt)


def audited_executable(values):
    """Observe CPython's explicit executable or its platform-specific argv."""
    if values[0] is not None:
        return os.fsdecode(values[0])
    command = values[1]
    if isinstance(command, (list, tuple)):
        return os.fsdecode(command[0]) if command else ''
    if isinstance(command, str):
        match = re.match(r'^\s*(?:"([^"]+)"|([^\s"]+))', command)
        return (match.group(1) or match.group(2)) if match else ''
    return ''  # Unresolved audit data cannot qualify as an intended native RED.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--implementation', choices=('original', 'patched-source'), required=True)
    args = parser.parse_args()
    os.environ['MARKER_TIMING_DIR'] = str(args.out.resolve())
    if sys.flags.optimize or not __debug__:
        emit('blocked', reason='assertion controls disabled by optimization')
        return 2
    if sys.platform != 'win32':
        emit('blocked', reason='real native Windows required')
        return 2
    sys.path.insert(0, str(args.repo.resolve()))
    from tests.scripts.desktop_update import test_desktop_update_windows_marker as source
    receipt = source_receipt(source, args.repo)
    emit('no_shell_source', implementation=args.implementation, **receipt)
    require_source(args.implementation, receipt)
    _creation_time = source._creation_time
    launches = []
    def audit(event, values):
        if event == 'subprocess.Popen':
            launches.append(audited_executable(values))
    sys.addaudithook(audit)  # Observe real process creation; does not block or replace it.
    emit('no_shell_start', implementation=args.implementation, **receipt)
    start = time.perf_counter_ns()
    identity = _creation_time(os.getpid())
    emit('no_shell_observed', implementation=args.implementation, identity=identity,
         elapsed_ns=time.perf_counter_ns() - start, subprocess_executables=launches, **receipt)
    assert not launches, f'creation identity spawned real subprocesses: {launches}'
    emit('no_shell_complete', implementation=args.implementation, **receipt)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
