"""Bounded original-source Windows x64 diagnostics, NOT full native acceptance."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET

SUBJECT = '2b682fffaa6c06fb700df0d32e21c0b4ac7370e6'
HELPER = 'tests/scripts/desktop_update/test_desktop_update_windows_marker.py'
HELPER_SHA = '2cb44bc565731abd75eea33376eb250065ce97471aee7b5acf83274145e6c36f'
BODY = 'tests/scripts/desktop_update/test_desktop_update_windows_marker_bodies.py'
CASE = 'test_marker_bodies_are_parsed_positionally_like_every_other_reader[crlf-bom-v2]'
HERE = Path(__file__).resolve().parent


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo, timeout=20).decode().strip()


def source_guard(repo):
    require(git(repo, 'rev-parse', 'HEAD') == SUBJECT, 'Wrong immutable subject HEAD')
    git(repo, 'diff', '--exit-code', 'HEAD', '--')
    hashes = json.loads((HERE / 'subject-sha256.json').read_text())
    for name, expected in hashes.items():
        blob = subprocess.check_output(['git', 'show', SUBJECT + ':' + name], cwd=repo, timeout=20)
        require(hashlib.sha256(blob).hexdigest() == expected, 'Wrong subject blob: ' + name)
        # Git applies ONLY its tracked clean/eol attributes; arbitrary normalization is forbidden.
        require(git(repo, 'hash-object', '--path=' + name, name) ==
                git(repo, 'rev-parse', SUBJECT + ':' + name), 'Wrong disk source: ' + name)
    require(digest(repo / HELPER) == HELPER_SHA, 'Original CIM helper bytes changed')
    return hashes


def resolve_native_bash(program_files):
    require(program_files.is_absolute(), 'Program Files must be an absolute path')
    git_root = program_files.resolve() / 'Git'
    executable = (git_root / 'bin' / 'bash.exe').resolve()
    require(executable.is_file() and executable.is_relative_to(git_root) and
            not {'system32', 'syswow64', 'wsl'} & {part.lower() for part in executable.parts},
            'Native Git Bash is required; no PATH/System32/WSL fallback')
    return executable


def clean_env(folder):
    allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'PROGRAMFILES',
               'PROGRAMFILES(X86)', 'PROGRAMW6432', 'PROGRAMDATA', 'CI', 'GITHUB_ACTIONS'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    for name in ('home', 'appdata', 'localappdata', 'temp', 'hermes'):
        (folder / name).mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(folder / 'home'), USERPROFILE=str(folder / 'home'),
               APPDATA=str(folder / 'appdata'), LOCALAPPDATA=str(folder / 'localappdata'),
               TEMP=str(folder / 'temp'), TMP=str(folder / 'temp'), TMPDIR=str(folder / 'temp'),
               HERMES_HOME=str(folder / 'hermes'), HERMES_PYTHON=sys.executable,
               HERMES_TEST_WORKERS='2', HERMES_TEST_FILE_RETRIES='0',
               HERMES_TEST_FILE_TIMEOUT='300', TZ='UTC', PYTHONUTF8='1',
               LANG='C.UTF-8', LC_ALL='C.UTF-8', PYTHONHASHSEED='0')
    return env


def job_contain_self():
    """Worker joins a kill-on-close job BEFORE launching any diagnostic child."""
    from ctypes import wintypes as w

    class Basic(ctypes.Structure):
        _fields_ = [('user', ctypes.c_int64), ('job', ctypes.c_int64), ('flags', w.DWORD),
                    ('min', ctypes.c_size_t), ('max', ctypes.c_size_t), ('active', w.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]

    class Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ('r', 'w', 'o', 'rb', 'wb', 'ob')]

    class Limits(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', Io), ('process', ctypes.c_size_t),
                    ('job', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                    ('peak_job', ctypes.c_size_t)]

    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateJobObjectW.argtypes = (ctypes.c_void_p, w.LPCWSTR)
    k.CreateJobObjectW.restype = w.HANDLE
    k.SetInformationJobObject.argtypes = (w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD)
    k.SetInformationJobObject.restype = w.BOOL
    k.GetCurrentProcess.restype = w.HANDLE
    k.AssignProcessToJobObject.argtypes = (w.HANDLE, w.HANDLE)
    k.AssignProcessToJobObject.restype = w.BOOL
    handle = k.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = Limits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
    if not k.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not k.AssignProcessToJobObject(handle, k.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    return handle  # Retain until worker exit: OS closes it and kills surviving descendants.


def worker(spec_path):
    spec = json.loads(Path(spec_path).read_text())
    handle = job_contain_self() if os.name == 'nt' else None
    save(Path(spec['ready']), {'contained': True, 'pid': os.getpid(),
                               'mechanism': 'kill-on-close-job' if handle else 'posix-session'})
    try:
        code = subprocess.call(spec['command'], cwd=spec['cwd'])
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 2
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # Windows closes the job handle even on error/timeout/parent termination.


def execute(command, repo, folder, env, budget=300):
    folder.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    reserve = min(30, budget / 4)
    spec = folder / 'worker-spec.json'
    ready = folder / 'containment.json'
    save(spec, {'command': command, 'cwd': str(repo), 'ready': str(ready)})
    result = {'outer_budget_seconds': budget, 'outer_timeout': False, 'command': command}
    with (folder / 'diagnostic.log').open('w', encoding='utf-8') as log:
        proc = subprocess.Popen([sys.executable, str(HERE / 'controller.py'), '--worker', str(spec)],
                                cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=os.name != 'nt')
        result['owned_pid'] = proc.pid
        try:
            result['returncode'] = proc.wait(timeout=max(.01, budget - reserve))
        except subprocess.TimeoutExpired:
            result['outer_timeout'] = True
        finally:
            if os.name == 'nt' and proc.poll() is None:
                # Terminate the exact owned Popen HANDLE, not a recycled numeric PID.
                # Worker job closure kills its descendants, including detached children.
                proc.kill()
                result['cleanup'] = 'owned-worker-handle-termination/job-close'
            elif os.name != 'nt':
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            result['returncode'] = proc.wait(timeout=max(.01, budget - (time.monotonic() - start)))
    result['elapsed_seconds'] = time.monotonic() - start
    result['containment'] = json.loads(ready.read_text()) if ready.exists() else None
    save(folder / 'process.json', result)
    require(result['containment'] is not None, 'Worker failed to establish owned-tree containment')
    require(result['elapsed_seconds'] <= budget, 'Outer envelope including cleanup exceeded')
    return result


def records(folder):
    return [json.loads(line) for p in sorted(folder.glob('timing-*.jsonl'))
            for line in p.read_text(encoding='utf-8').splitlines()]


def classify_red(code, data):
    observed = [r for r in data if r['event'] == 'no_shell_observed']
    require(code == 1 and len(observed) == 1, 'Not the intended no-shell RED')
    record = observed[0]
    require(record.get('implementation') == 'original' and
            record.get('helper_source_sha256') == HELPER_SHA and
            re.fullmatch(r'[1-9][0-9]*\.[0-9]{3}', record.get('identity', '')) is not None,
            'RED requires a returned real creation identity')
    launches = record.get('subprocess_executables', [])
    require(any(Path(p.replace('\\', '/')).name.lower() in ('powershell.exe', 'powershell')
                for p in launches), 'RED requires actual Popen PowerShell audit evidence')
    return True


def archive_subject(repo, subject, destination):
    """Export immutable Git bytes locally; never use checkout files or upload source."""
    require(re.fullmatch(r'[0-9a-f]{40}', subject or '') is not None,
            '--green-subject must be a reviewed immutable lowercase 40-hex commit')
    require(git(repo, 'rev-parse', subject + '^{commit}') == subject, 'Wrong GREEN commit')
    destination = destination.resolve()
    require(not destination.is_relative_to(repo.resolve()), 'Archive must be outside source checkout')
    destination.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryFile() as archive:
        subprocess.run(['git', 'archive', '--format=tar', subject], cwd=repo,
                       stdout=archive, check=True, timeout=60)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as source:
            source.extractall(destination, filter='data')


def classify_green(code, data, repo=None):
    from native_no_shell import SOURCE_HASHES
    require(code == 0, 'Actual patched-source GREEN failed')
    expected = ('no_shell_source', 'no_shell_start', 'no_shell_observed', 'no_shell_complete')
    selected = [r for r in data if r['event'] in expected]
    require([r['event'] for r in selected] == list(expected), 'Incomplete/duplicate GREEN receipts')
    for record in selected:
        require(record.get('implementation') == 'patched-source' and
                record.get('helper_source_sha256') == SOURCE_HASHES['patched-source'] and
                record.get('helper_module') == 'tests.scripts.desktop_update.test_desktop_update_windows_marker' and
                bool(record.get('helper_path')), 'Wrong actual GREEN source binding')
    require(len({r['helper_path'] for r in selected}) == 1, 'GREEN source path changed')
    if repo is not None:
        require(Path(selected[0]['helper_path']).resolve() == (repo / HELPER).resolve(),
                'GREEN receipt is not from the archived final subject')
    observed = selected[2]
    require(re.fullmatch(r'[1-9][0-9]*\.[0-9]{3}', observed.get('identity', '')) is not None and
            observed.get('subprocess_executables') == [], 'GREEN requires returned identity and no Popen')
    return True


def classify_owned_denial(code, data, green_repo):
    """A detached candidate/blocked scan cannot satisfy actual-source denial."""
    from native_no_shell import SOURCE_HASHES
    complete = [r for r in data if r['event'] == 'controls_complete']
    denied = [r for r in data if r['event'] == 'control_pass' and
              r.get('control') == 'inaccessible-pid']
    require(code == 0 and len(complete) == 1 and complete[0].get('blocked_controls') == [] and
            len(denied) == 1 and not any(r['event'] == 'control_blocked' for r in data),
            'Native comparison/control unavailable or failed; inspect raw JSONL/log')
    record = denied[0]
    require(record.get('fixture') == 'owned-child-current-thread-anonymous' and
            record.get('helper_source_sha256') == SOURCE_HASHES['patched-source'] and
            record.get('helper_module') == 'marker_owned_denial_source' and
            record.get('helper_path') == record.get('failure_path') and
            Path(record.get('helper_path', '')).resolve() == (green_repo / HELPER).resolve() and
            record.get('failure_function') == '_creation_time' and
            type(record.get('failure_line')) is int and record['failure_line'] > 0 and
            record['failure_line'] == record.get('denied_raise_line'),
            'Denied helper did not fail in the exact archived source OpenProcess branch')
    require(record.get('native_winerror') == 5 and record.get('candidate_winerror') == 5 and
            record.get('query_access') == 0x1000 and record.get('thread_token_before') == 1008 and
            record.get('thread_token_after') == 1008 and record.get('recovered') is True and
            type(record.get('owner_pid')) is int and record['owner_pid'] > 0 and
            record.get('identity_before') == record.get('identity_after') and
            re.fullmatch(r'[1-9][0-9]*\.[0-9]{3}', record.get('identity_before', '')) is not None,
            'Missing native denial or recovered live owned-child identity')
    return True


def run_native_diagnostics(repo, green_repo, out):
    failures = []
    for label, script, target, implementation in (
        ('native-red', 'native_no_shell.py', repo, 'original'),
        ('native-equality', 'native_controls.py', repo, None),
        ('patched-source-green', 'native_no_shell.py', green_repo, 'patched-source'),
    ):
        folder = out / label
        command = [sys.executable, str(HERE / script), '--repo', str(target), '--out', str(folder)]
        if implementation:
            command += ['--implementation', implementation]
        else:
            command += ['--denial-repo', str(green_repo)]
        try:
            result = execute(command, target, folder, clean_env(folder / 'sandbox'))
            require(not result['outer_timeout'], label + ' timed out')
            data = records(folder)
            if label == 'native-red':
                classify_red(result['returncode'], data)
            elif label == 'patched-source-green':
                classify_green(result['returncode'], data, target)
            else:
                classify_owned_denial(result['returncode'], data, green_repo)
        except Exception as error:
            failures.append(label + ': ' + str(error))
    return failures  # Blocked/failed equality must not suppress final-source GREEN.


def classify_timing(code, folder):
    require(code == 0, 'Canonical diagnostic failed')
    tree = ET.parse(folder / 'junit.xml').getroot()
    cases = list(tree.iter('testcase'))
    suites = list(tree.iter('testsuite'))
    require(len(cases) == 1 and cases[0].get('name') == CASE and
            cases[0].get('classname') == BODY[:-3].replace('/', '.'), 'Wrong diagnostic test identity')
    require(sum(int(s.get('tests', 0)) for s in suites) == 1 and
            not any(list(tree.iter(tag)) for tag in ('failure', 'error', 'skipped')),
            'Diagnostic contains failure/error/skip or wrong declared totals')
    data = records(folder)
    collection = [r for r in data if r['event'] == 'collection']
    require(len(collection) == 1 and collection[0]['selected_nodeids'] == [BODY + '::' + CASE],
            'Plugin missing or wrong selected set')
    require(collection[0]['helper_source_sha256'] == HELPER_SHA and
            collection[0]['platform'] == 'win32' and collection[0]['machine'].lower() in ('amd64', 'x86_64'),
            'Wrong helper or native architecture')
    aliases = collection[0].get('wrapped_aliases', [])
    require({BODY[:-3].replace('/', '.') + '.' + helper for helper in ('_creation_time', '_run')}
            <= set(aliases), 'Missing actual marker-bodies helper aliases')
    starts = {r['invocation']: r for r in data if r['event'] == 'helper_start'}
    ends = {r['invocation']: r for r in data if r['event'] == 'helper_end'}
    require(starts and set(starts) == set(ends) and
            {r['helper'] for r in starts.values()} == {'_creation_time', '_run'} and
            not any(r['event'] == 'helper_error' for r in data), 'Incomplete/error helper attribution')
    phases = [r for r in data if r['event'] == 'phase_end']
    require([r['phase'] for r in phases] == ['setup', 'call', 'teardown'] and
            all(r['outcome'] == 'passed' for r in phases), 'Incomplete pytest phases')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--green-repo', type=Path)
    parser.add_argument('--green-subject')
    parser.add_argument('--guard-only', action='store_true')
    parser.add_argument('--worker')
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
        return 2
    require(args.repo is not None and args.out is not None, '--repo and --out required')
    repo, out = args.repo.resolve(), args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    summary = {'accepted': False, 'scope': 'diagnostic-only-NOT-full-60-case-acceptance',
               'subject': SUBJECT, 'run_id': os.environ.get('GITHUB_RUN_ID'),
               'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT')}
    plugin = repo / 'marker_timing_plugin.py'
    copied_plugin = False
    try:
        source = source_guard(repo)
        save(out / 'source-before.json', source)
        expected = json.loads((HERE / 'diagnostic-sha256.json').read_text())
        for name, value in expected.items():
            require(digest(HERE / name) == value, 'Prepared harness byte mismatch: ' + name)
        save(out / 'harness.json', {p.name: digest(p) for p in HERE.iterdir() if p.is_file()})
        if args.guard_only:
            summary['accepted'] = True
            return 0
        require(os.name == 'nt' and sys.platform == 'win32' and platform.machine().lower() in ('amd64', 'x86_64')
                and ctypes.sizeof(ctypes.c_void_p) == 8, 'Real native Windows x64 required')
        save(out / 'host.json', {'platform': sys.platform, 'machine': platform.machine(),
                                 'pointer_bytes': ctypes.sizeof(ctypes.c_void_p), 'python': sys.version})
        require(not plugin.exists(), 'Refuse to overwrite an existing diagnostic plugin')
        shutil.copyfile(HERE / plugin.name, plugin)
        copied_plugin = True
        timing = out / 'original-one-case'
        env = clean_env(timing / 'sandbox')
        # run_tests.sh intentionally strips caller PYTEST_PLUGINS. A tiny interpreter
        # adapter restores ONLY this pinned plugin AFTER env -i, without editing source.
        shim = out / 'diagnostic-python.sh'
        shim.write_text('#!/usr/bin/env bash\nexport PYTEST_PLUGINS=marker_timing_plugin\nexec ' +
                        shlex.quote(sys.executable.replace('\\', '/')) + ' "$@"\n', encoding='utf-8')
        shim.chmod(0o700)
        env['HERMES_PYTHON'] = str(shim).replace('\\', '/')
        bash = resolve_native_bash(Path(os.environ.get('ProgramFiles', 'C:/Program Files')))
        command = [str(bash), 'scripts/run_tests.sh', BODY, '--', '-k', 'crlf-bom-v2',
                   '-o', 'marker_timing_dir=' + str(timing), '--junitxml=' + str(timing / 'junit.xml'),
                   '-vv', '--capture=tee-sys']
        result = execute(command, repo, timing, env)
        from summarize import summarize
        save(timing / 'timing-summary.json', summarize(timing))
        require(not result['outer_timeout'], 'Original diagnostic timed out; retain partial timings')
        classify_timing(result['returncode'], timing)
        require(args.green_repo is not None and args.green_subject is not None,
                '--green-repo and reviewed --green-subject required for actual-source GREEN')
        from native_no_shell import SOURCE_HASHES
        # Full archived source is temporary, outside both checkouts and upload inventory.
        with tempfile.TemporaryDirectory(prefix='marker-green-', dir=out.parent) as temporary:
            green_repo = Path(temporary) / 'source'
            require(not green_repo.is_relative_to(repo), 'GREEN archive must be outside original checkout')
            archive_subject(args.green_repo.resolve(), args.green_subject, green_repo)
            green_hash = digest(green_repo / HELPER)
            require(green_hash == SOURCE_HASHES['patched-source'], 'Wrong final-subject actual helper bytes')
            receipt = dict(subject=args.green_subject, helper_source_sha256=green_hash,
                           helper_path=str(green_repo / HELPER), implementation='patched-source',
                           status='prepared-not-yet-verified')
            save(out / 'green-source.json', receipt)
            failures = run_native_diagnostics(repo, green_repo, out)
            receipt['after_sha256'] = digest(green_repo / HELPER)
            require(receipt['after_sha256'] == green_hash, 'Archived GREEN helper bytes changed')
            receipt['status'] = ('failed' if any(f.startswith('patched-source-green:') for f in failures)
                                 else 'actual-source-green')
            save(out / 'green-source.json', receipt)
            summary['diagnostic_failures'] = failures
            require(not failures, '; '.join(failures))
        summary['accepted'] = True
        return 0
    except Exception as error:
        summary['error'] = str(error)
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if copied_plugin:
            plugin.unlink()
        try:
            save(out / 'source-after.json', source_guard(repo))
        except Exception as error:
            summary['accepted'] = False
            summary['source_after_error'] = str(error)
        save(out / 'summary.json', summary)
        # A guard failure after otherwise successful diagnostics must propagate.
        if 'source_after_error' in summary:
            raise SystemExit(1)


if __name__ == '__main__':
    raise SystemExit(main())
