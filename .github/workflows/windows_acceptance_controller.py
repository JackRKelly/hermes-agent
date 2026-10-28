"""Native Windows x64/ARM64 canonical whole-file acceptance and mutation challenge.

--list and --self-test do not execute Windows cases and allow real macOS Python.
All execution requires native Windows, exact subject SHA and Python 3.14.9.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

FILES = [
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_outcome.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_lineage.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_release.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_supervision.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_bodies.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lifecycle.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_custodian.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_protocol.py',
]
FIXTURE = 'tests/scripts/desktop_update/test_r6_checkout_fixture.py'
MARKER_LOCK = 'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py'
ALL_FILES = [*FILES, MARKER_LOCK, FIXTURE]
POWERSHELL = 'scripts/desktop-update/windows.ps1'
TIMESTAMP_FILE = 'tests/scripts/desktop_update/test_desktop_update_windows_commit_release.py'
TIMESTAMP_NAME = 'test_result_finished_at_is_stamped_after_the_r6_release_wait'
TIMESTAMP = TIMESTAMP_FILE + '::' + TIMESTAMP_NAME
HASHES = {
    'tests/scripts/desktop_update/conftest.py': 'd3f427f92034b64755a846c185d31ac8e55231dc5664cc26e098f5fe1bffb82b',
    'scripts/desktop-update/windows.ps1': 'b9e83ea4819a1a4bcedd023f2b3caf145f303c209bbb88404834c8f16e1fb760',
    'scripts/desktop-update/marker.ps1': 'fd9bb4a536f0dbf37c54d1e902eef0635e2c04913dd631e91b468696ce526299',
    'scripts/desktop-update/marker-claim.ps1': '43fabb7a293eba12e8044d26447952891f9a481b61e76205326de9e0b1df11cf',
    'scripts/desktop-update/update-job.ps1': 'a2f599925aca103b5204e6c9de44a94efa9dd0dcba0cf10af8e381e52643eda6',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_outcome.py': '225de09a1420cf406964a6e313d3a9e235c3149ac2fd7c42a9cc5f6bbafc67aa',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker.py': '2cb44bc565731abd75eea33376eb250065ce97471aee7b5acf83274145e6c36f',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_lineage.py': 'd9199d78cee359d015ebb571cdefa94212217264c772e270e4105ba983651a76',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_release.py': '5d7ddb97bd93d21487e6ff18fe7ee83edf149bbdb79c4e5f19c7ef59aaf72493',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_supervision.py': 'f1587fb0129b92623c72c7ad097bf918e710164302daf41bffea3cce1a101d57',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_bodies.py': '58c35527edc253fb8daa4c8ba5a248582c08ef72d537811792b79d2657cace89',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lifecycle.py': 'f6f8ca08272e68db0a396344f39be82878132bb3caac9837f7d2b7defd9f1b1c',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_custodian.py': '443d2ad33d33052e0f1ad15e2e856fb36b8ab863a128da15fbaa265e23d39964',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_protocol.py': '0659269e51cea52c893aaa372d6669ad35fbaadb2ebae5ee3d8d43f508ec4c74',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py': '2d0b82a6454bf8377d4446b88e027c65d75ca345e39279aee4fe45486c6d30e8',
    'tests/scripts/desktop_update/test_r6_checkout_fixture.py': '378e9d0691ec996c9e52e8615af98d165bdf321ac7f0b4dc1c079bdb1c0276d1',
}
RESTAMP = (
    '        if ($script:MarkerReleaseWaited -and [System.IO.File]::Exists($ResultPath)) {\n'
    '            Write-Result ($finalCode -eq 0) $finalCode $finalMsg $manualAction\n'
    '        }\n'
)
ASSERTION = "assert result['ok'] is True and result['finished_at'] >= int(holders[0].released), result"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, timeout=60)


def attributes(root):
    raw = git(root, 'check-attr', '-z', 'text', 'eol', '--', *HASHES).decode().split('\0')
    attrs = {name: {} for name in HASHES}
    for i in range(0, len(raw) - 1, 3):
        name, key, value = raw[i:i + 3]
        attrs[name][key] = value
    return attrs


def projected(data, attrs):
    # Only the repository's explicit text/eol contract permits normalization.
    require(attrs.get('text') == 'set' and attrs.get('eol') in ('lf', 'crlf'),
            f'Unexpected Git attributes: {attrs}')
    lf = data.replace(b'\r\n', b'\n')
    require(b'\r' not in lf, 'Bare carriage return in scoped text')
    if attrs['eol'] == 'lf':
        require(data == lf, 'LF-pinned file contains CRLF')
        return lf
    return lf.replace(b'\n', b'\r\n')


def verify_hashes(root, attrs, subject=None):
    details = {}
    for name, expected in HASHES.items():
        data = (root / name).read_bytes()
        digest = sha(projected(data, attrs[name]))
        require(digest == expected, f'Scoped hash mismatch: {name}: {digest}')
        if subject:
            blob = git(root, 'show', subject + ':' + name)
            require(sha(projected(blob, attrs[name])) == expected, f'Git blob mismatch: {name}')
        details[name] = {'sha256': digest, 'disk_sha256': sha(data), 'attributes': attrs[name]}
    return details


def source_guard(root, subject):
    require(re.fullmatch(r'[0-9a-f]{40}', subject) is not None, 'Subject must be a full SHA; bind placeholder before commit')
    require(git(root, 'rev-parse', 'HEAD').decode().strip() == subject, 'Wrong subject HEAD')
    git(root, 'diff', '--exit-code', 'HEAD', '--')
    attrs = attributes(root)
    return attrs, verify_hashes(root, attrs, subject)


def sandbox(folder, root):
    # Allowlisted locations only: never inherit providers, tokens, profile state,
    # activation, PYTEST_ADDOPTS, external plugins or Git configuration env vars.
    allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'PROGRAMFILES',
               'PROGRAMFILES(X86)', 'PROGRAMW6432', 'PROGRAMDATA', 'CI', 'GITHUB_ACTIONS'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    for name in ('home', 'appdata', 'localappdata', 'temp', 'hermes'):
        (folder / name).mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(folder / 'home'), USERPROFILE=str(folder / 'home'),
               APPDATA=str(folder / 'appdata'), LOCALAPPDATA=str(folder / 'localappdata'),
               HERMES_HOME=str(folder / 'hermes'), TEMP=str(folder / 'temp'),
               TMP=str(folder / 'temp'), TMPDIR=str(folder / 'temp'),
               HERMES_PYTHON=sys.executable, HERMES_TEST_WORKERS='2',
               HERMES_TEST_FILE_RETRIES='0', HERMES_TEST_FILE_TIMEOUT='300',
               TZ='UTC', LANG='C.UTF-8', LC_ALL='C.UTF-8', PYTHONHASHSEED='0',
               PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONPATH=str(root),
               OPENAI_API_KEY='', OPENROUTER_API_KEY='', NOUS_API_KEY='')
    return env


def collect(root, env):
    proc = subprocess.run([sys.executable, '-m', 'pytest', *ALL_FILES, '--collect-only', '-q'],
                          cwd=root, env=env, capture_output=True, text=True, timeout=60, check=False)
    require(proc.returncode == 0, proc.stdout + proc.stderr)
    nodes = [line.strip() for line in proc.stdout.splitlines()
             if any(line.startswith(name + '::') for name in ALL_FILES)]
    require(len(nodes) == len(set(nodes)), 'Duplicate collected nodes')
    counts = {name: sum(n.startswith(name + '::') for n in nodes) for name in ALL_FILES}
    require(all(counts.values()) and sum(counts[n] for n in FILES) == 41
            and counts[MARKER_LOCK] == 13 and counts[FIXTURE] == 5, f'Wrong collection: {counts}')
    selector = subprocess.run([sys.executable, 'scripts/ci/list_os_marked_tests.py', 'windows'],
                              cwd=root, env=env, capture_output=True, text=True, timeout=60, check=True)
    require(set(FILES) <= set(selector.stdout.splitlines()), 'Windows selector omitted partition files')
    require(TIMESTAMP in nodes, 'Timestamp node missing')
    return [TIMESTAMP] + [node for node in nodes if node != TIMESTAMP]


def junit(path):
    tree = ET.parse(path).getroot()
    cases = list(tree.iter('testcase'))
    suites = list(tree.iter('testsuite'))
    require(bool(suites), 'Missing JUnit testsuite')
    # Inspect actual cases AND declared totals; setup errors/skips are not passes.
    counts = {'tests': len(cases), 'failures': len(list(tree.iter('failure'))),
              'errors': len(list(tree.iter('error'))), 'skipped': len(list(tree.iter('skipped')))}
    for key, actual in counts.items():
        declared = sum(int(s.get(key, '0')) for s in suites)
        require(declared == actual, f'JUnit declared {key}={declared}, actual={actual}')
    return counts, cases


def timestamp_mismatch(text):
    match = re.search(r'^E\s+assert \(True is True and (\d+) >= (\d+)\)', text, re.MULTILINE)
    if match is None:
        raise RuntimeError('No actual True result / numeric timestamp mismatch in rewritten assertion')
    finished, released = map(int, match.groups())
    require(finished < released, 'Expected a stale result, not another line-34 failure')
    return {'finished_at': finished, 'release_lower_bound_integer': released, 'result_ok': True}


def classify(path, code, expected_count=1, node=None, red=False):
    counts, cases = junit(path)
    expected = {'tests': expected_count, 'failures': int(red), 'errors': 0, 'skipped': 0}
    require(counts == expected, f'JUnit outcomes {counts}, expected {expected}')
    require(code == (1 if red else 0), f'Unexpected pytest/runner exit {code}')
    if node:
        require(len(cases) == 1 and cases[0].get('name') == node.split('::')[-1], 'Wrong JUnit testcase name')
        parts = node.split('::')
        classname = '.'.join([parts[0].removesuffix('.py').replace('/', '.'), *parts[1:-1]])
        require(cases[0].get('classname') == classname,
                'Wrong JUnit testcase class/file')
    if red:
        failure = cases[0].find('failure')
        if failure is None:
            raise RuntimeError('Expected timestamp failure element')
        text = failure.text or ''
        # Earlier passed code may appear in a long traceback. Only the actual
        # > failure source line, the terminal location AND assertion type count.
        arrows = [line[1:].strip() for line in text.splitlines() if line.startswith('>')]
        require(arrows == [ASSERTION], f'Not the timestamp assertion: {arrows}')
        require(re.search(r'(?:^|[/\\])test_desktop_update_windows_commit_release\.py:34: AssertionError', text) is not None,
                'Missing exact timestamp assertion location (line 34)')
        require('AssertionError' in failure.get('message', ''), 'Not an assertion failure')
        timestamp_mismatch(text)
    return counts


def execute(command, root, folder, env, timeout=300):
    folder.mkdir(parents=True, exist_ok=True)
    start, clock = time.time(), time.monotonic()
    result = {'command': command, 'cwd': str(root), 'started_epoch': start,
              'outer_budget_seconds': timeout, 'outer_timeout': False, 'cleanup': None}
    with (folder / 'pytest.log').open('w', encoding='utf-8') as log:
        proc = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
        result['owned_pid'] = proc.pid
        try:
            result['returncode'] = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            result['outer_timeout'] = True
            # Exact owned PID tree only; never kill by executable image name.
            kill = subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                                  capture_output=True, text=True, timeout=15, check=False)
            result['cleanup'] = {'returncode': kill.returncode, 'stdout': kill.stdout, 'stderr': kill.stderr}
            result['returncode'] = proc.wait(timeout=15)
    result['elapsed_seconds'] = time.monotonic() - clock
    save(folder / 'process.json', result)
    return result


def run_case(root, folder, node, red=False):
    result = {'node': node, 'accepted': False, 'expected_mutation_red': red}
    try:
        env = sandbox(folder / 'sandbox', root)
        command = [sys.executable, '-m', 'pytest', node, '-x', '-vv', '--tb=long', '--durations=0',
                   '-o', 'faulthandler_timeout=60', '--basetemp', str(folder / 'tmp'),
                   '--junitxml', str(folder / 'junit.xml')]
        result.update(execute(command, root, folder, env))
        require(not result['outer_timeout'], 'Existing 300s outer deadline exceeded')
        result['junit'] = classify(folder / 'junit.xml', result['returncode'], node=node, red=red)
        if red:
            logs = list((folder / 'tmp').rglob('desktop-update-handoff.log'))
            require(len(logs) == 1, f'Expected one retained handoff log; got {len(logs)}')
            text = logs[0].read_text(encoding='utf-8-sig')
            require('keeping the update marker' in text, 'No real R6 wait in retained log')
            require('removed update marker (owned)' in text, 'No owned marker removal in retained log')
            require(not (logs[0].parent.parent / '.hermes-update-in-progress').exists(), 'Owned marker remains')
            _, cases = junit(folder / 'junit.xml')
            failure = cases[0].find('failure')
            if failure is None:
                raise RuntimeError('Timestamp failure element disappeared')
            result['timestamp_evidence'] = timestamp_mismatch(failure.text or '')
            published = json.loads((logs[0].parent.parent / '.hermes-update-result.json').read_text(encoding='utf-8-sig'))
            require(published['ok'] is True and published['finished_at'] == result['timestamp_evidence']['finished_at'],
                    'Retained actual result does not match timestamp assertion evidence')
            result['handoff_log'] = str(logs[0])
            result['handoff_log_sha256'] = sha(logs[0].read_bytes())
            result['release_ack_proof'] = 'Unchanged test passed holders[0].finish() at line 30 and failed only line 34'
        result['accepted'] = True
    except Exception as exc:
        result['error'] = repr(exc)
    folder.mkdir(parents=True, exist_ok=True)
    save(folder / 'result.json', result)
    print(json.dumps(result), flush=True)
    return result


def cache_guard(root, name):
    cache = root / 'test_durations.json'
    data = json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else {}
    require(isinstance(data, dict) and name not in data, f'Cached timeout scaler entry forbidden: {name}')
    return {'cache_entry_absent': name, 'effective_file_timeout_seconds': 300}


def case_identity(node):
    parts = node.split('::')
    return ('.'.join([parts[0].removesuffix('.py').replace('/', '.'), *parts[1:-1]]), parts[-1])


def match_inventory(path, expected):
    _, cases = junit(path)
    actual = [(c.get('classname'), c.get('name')) for c in cases]
    wanted = [case_identity(n) for n in expected]
    require(len(actual) == len(set(actual)) and sorted(actual) == sorted(wanted),
            f'JUnit inventory mismatch: actual={actual}, expected={wanted}')
    return list(expected)


def aggregate(results, nodes):
    actual = [n for r in results for n in r.get('nodes', [])]
    require(len(actual) == len(set(actual)) and sorted(actual) == sorted(nodes),
            'Missing/duplicate whole-file case inventory')
    require(len(results) == len(ALL_FILES) and {r['file'] for r in results} == set(ALL_FILES),
            'Missing/duplicate whole-file executions')
    return {'collected': len(nodes), 'completed': len(actual), 'unique': len(set(actual)),
            'passed': sum(len(r['nodes']) for r in results if r['accepted']),
            'whole_files': len(results), 'retries': 0, 'skipped': 0}


def run_file(root, folder, name, nodes):
    result = {'file': name, 'accepted': False, 'nodes': []}
    try:
        env = sandbox(folder / 'sandbox', root)
        result.update(cache_guard(root, name))
        command = [str(resolve_native_bash(Path(os.environ['ProgramFiles']))), 'scripts/run_tests.sh',
                   '-j', '1', '--file-retries', '0', '--file-timeout', '300', '--fail-on-skip', name,
                   '--', '-vv', '--tb=long', '--basetemp', str(folder / 'tmp'),
                   '--junitxml', str(folder / 'junit.xml')]
        result.update(execute(command, root, folder, env))
        require(not result['outer_timeout'] and result['elapsed_seconds'] < 300,
                'Whole-file wall exceeded unchanged 300s budget')
        result['junit'] = classify(folder / 'junit.xml', result['returncode'], expected_count=len(nodes))
        result['nodes'] = match_inventory(folder / 'junit.xml', nodes)
        result['accepted'] = True
    except Exception as exc:
        result['error'] = repr(exc)
    folder.mkdir(parents=True, exist_ok=True)
    save(folder / 'result.json', result)
    print(json.dumps(result), flush=True)
    return result


def mutate(data):
    newline = b'\r\n' if b'\r\n' in data else b'\n'
    require(b'\r' not in data.replace(b'\r\n', b''), 'Mixed/bare CR in mutation input')
    block = RESTAMP.encode().replace(b'\n', newline)
    require(data.count(block) == 1, 'Expected exactly one documented post-R6 restamp block')
    before, after = data.split(block)
    require(before.rstrip().endswith(b'# finish time (unless something already consumed it).'), 'Wrong restamp context')
    require(b'Invoke-MarkerRelease' in before and b'Write-Result ($finalCode -eq 0)' in before,
            'Initial result and R6 release must remain')
    return before + after


def relative_key(path, root):
    return path.relative_to(root).as_posix()


def resolve_native_bash(program_files):
    require(program_files.is_absolute(), 'Program Files must be an absolute path')
    git_root = (program_files / 'Git').resolve()
    executable = (git_root / 'bin' / 'bash.exe').resolve()
    require(executable.is_file() and executable.is_relative_to(git_root), 'Native Git Bash is required')
    return executable


def manifest(root):
    return {relative_key(p, root): sha(p.read_bytes()) for p in root.rglob('*') if p.is_file()}


def mutation(root, output, subject, attrs):
    folder = output / 'mutation'
    folder.mkdir()
    archive = folder / 'subject.tar'
    with archive.open('wb') as stream:
        subprocess.run(['git', 'archive', '--format=tar', subject], cwd=root, stdout=stream, check=True, timeout=60)
    copy = folder / 'subject'
    copy.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(copy, filter='data')
    verify_hashes(copy, attrs)
    before = manifest(copy)
    ps = copy / POWERSHELL
    ps.write_bytes(mutate(ps.read_bytes()))
    after = manifest(copy)
    changed = [name for name in before.keys() | after.keys() if before.get(name) != after.get(name)]
    require(changed == [POWERSHELL], f'Mutation changed unexpected paths: {changed}')
    save(folder / 'mutation.json', {'subject': subject, 'archive_sha256': sha(archive.read_bytes()),
                                  'changed_paths': changed, 'before_sha256': before[POWERSHELL],
                                  'after_sha256': after[POWERSHELL], 'test_sha256': after[TIMESTAMP_FILE]})
    return run_case(copy, folder / 'timestamp-red', TIMESTAMP, red=True)


class ControllerUnitTests(unittest.TestCase):
    """Synthetic XML/bytes only; these are NOT native Windows behavioral evidence."""
    def test_relative_keys_are_portable_without_os_spoofing(self):
        from pathlib import PurePosixPath, PureWindowsPath
        for kind, prefix in ((PurePosixPath, '/root'), (PureWindowsPath, 'C:/root')):
            with self.subTest(kind=kind):
                root = kind(prefix)
                self.assertEqual(relative_key(root / POWERSHELL, root), POWERSHELL)

    def test_git_bash_resolution_never_uses_bare_or_system32_bash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git_bash = root / 'Git' / 'bin' / 'bash.exe'
            git_bash.parent.mkdir(parents=True)
            git_bash.write_bytes(b'unit fixture only; never executed')
            self.assertEqual(resolve_native_bash(root), git_bash.resolve())
            git_bash.unlink()
            wsl = root / 'System32' / 'bash.exe'
            wsl.parent.mkdir()
            wsl.write_bytes(b'unit fixture only; never executed')
            with self.assertRaises(RuntimeError):
                resolve_native_bash(root)

    def test_junit_class_nodes_keep_all_class_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'junit.xml'
            suite = ET.Element('testsuite', tests='1', failures='0', errors='0', skipped='0')
            ET.SubElement(suite, 'testcase', name='test_case[param]', classname='tests.sample.TestOuter.TestInner')
            ET.ElementTree(suite).write(path)
            self.assertEqual(classify(path, 0, node='tests/sample.py::TestOuter::TestInner::test_case[param]')['tests'], 1)

    def test_whole_file_inventory_and_cache_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'junit.xml'
            nodes = ['tests/sample.py::TestOuter::test_case[a]', 'tests/sample.py::TestInner::test_case[b]']
            suite = ET.Element('testsuite', tests='2', failures='0', errors='0', skipped='0')
            for node in nodes:
                classname, name = case_identity(node)
                ET.SubElement(suite, 'testcase', name=name, classname=classname)
            ET.ElementTree(suite).write(path)
            self.assertEqual(match_inventory(path, nodes), nodes)
            for wrong in (nodes[:1], [nodes[0], nodes[0]], [n.replace('TestInner', 'TestWrong') for n in nodes]):
                with self.assertRaises(RuntimeError):
                    match_inventory(path, wrong)
            self.assertEqual(cache_guard(root, FILES[0])['effective_file_timeout_seconds'], 300)
            save(root / 'test_durations.json', {FILES[1]: 1000})
            cache_guard(root, FILES[0])  # Completed other files cannot inflate this file.
            save(root / 'test_durations.json', {FILES[0]: 1000})
            with self.assertRaises(RuntimeError):
                cache_guard(root, FILES[0])
            results = [{'file': f, 'nodes': [f + '::test_case'], 'accepted': True} for f in ALL_FILES]
            expected = [r['nodes'][0] for r in results]
            self.assertEqual(aggregate(results, expected)['whole_files'], 11)
            for bad in (results[:-1], results + results[:1], results[:-1] + results[:1]):
                with self.assertRaises(RuntimeError):
                    aggregate(bad, expected)

    def test_mutation_exact_and_guarded(self):
        context = b'Write-Result ($finalCode -eq 0)\nInvoke-MarkerRelease\n# finish time (unless something already consumed it).\n'
        for nl in (b'\n', b'\r\n'):
            source = (context + RESTAMP.encode() + b'unchanged\n').replace(b'\n', nl)
            self.assertEqual(mutate(source), (context + b'unchanged\n').replace(b'\n', nl))
            for bad in (source + RESTAMP.encode().replace(b'\n', nl), b'none', RESTAMP.encode()):
                with self.assertRaises(RuntimeError):
                    mutate(bad)

    def test_attributes_not_blind_normalization(self):
        self.assertEqual(projected(b'a\nb\n', {'text': 'set', 'eol': 'crlf'}), b'a\r\nb\r\n')
        for data, attrs in ((b'a\r\n', {'text': 'set', 'eol': 'lf'}),
                            (b'a\rb', {'text': 'set', 'eol': 'crlf'}), (b'a\n', {})):
            with self.assertRaises(RuntimeError):
                projected(data, attrs)

    def test_junit_accepts_only_exact_pass_and_actual_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'junit.xml'
            def xml(fail=0, errors=0, skip=0, body='', tests=1, name=TIMESTAMP_NAME):
                suite = ET.Element('testsuite', tests=str(tests), failures=str(fail), errors=str(errors), skipped=str(skip))
                case = ET.SubElement(suite, 'testcase', name=name,
                                     classname=TIMESTAMP_FILE.removesuffix('.py').replace('/', '.'))
                if fail:
                    ET.SubElement(case, 'failure', message='AssertionError').text = body
                if errors:
                    ET.SubElement(case, 'error').text = body
                if skip:
                    ET.SubElement(case, 'skipped')
                ET.ElementTree(suite).write(path)
            xml()
            self.assertEqual(classify(path, 0, node=TIMESTAMP)['tests'], 1)
            red = '>       ' + ASSERTION + '\nE       assert (True is True and 1 >= 2)\n' + TIMESTAMP_FILE + ':34: AssertionError'
            xml(fail=1, body=red)
            self.assertEqual(classify(path, 1, node=TIMESTAMP, red=True)['failures'], 1)
            # Critical trap: passed source contains target assertion, but > is a different failure.
            for body in (ASSERTION + '\n>       holders[0].finish()\n' + TIMESTAMP_FILE + ':34: AssertionError',
                         red.replace(':34:', ':33:'), red.replace(ASSERTION, 'assert False'),
                         red.replace('True is True', 'False is True'), red.replace('1 >= 2', '2 >= 1')):
                xml(fail=1, body=body)
                with self.assertRaises(RuntimeError):
                    classify(path, 1, node=TIMESTAMP, red=True)
            for kwargs in ({'skip': 1}, {'errors': 1}, {'tests': 2}):
                xml(skip=kwargs.get('skip', 0), errors=kwargs.get('errors', 0), tests=kwargs.get('tests', 1))
                with self.assertRaises(RuntimeError):
                    classify(path, 0, node=TIMESTAMP)
            xml(name='wrong')
            with self.assertRaises(RuntimeError):
                classify(path, 0, node=TIMESTAMP)
            xml()
            with self.assertRaises(RuntimeError):
                classify(path, 1, node=TIMESTAMP)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path)
    parser.add_argument('--subject')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--arch', choices=('x64', 'arm64'))
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        return int(not unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ControllerUnitTests)).wasSuccessful())
    if args.repo is None or not args.subject or args.output is None:
        parser.error('--repo --subject --output required')
    require(sys.version_info[:3] == (3, 14, 9), 'Exact Python 3.14.9 required')
    if not args.list:
        require(platform.system() == 'Windows' and os.name == 'nt', 'Native Windows required; no spoofing')
        require(args.arch is not None and platform.machine().lower() == {'x64': 'amd64', 'arm64': 'arm64'}[args.arch],
                'Native host architecture must match matrix')
        require(__import__('struct').calcsize('P') == 8, '64-bit Python required')
        require(__import__('sysconfig').get_platform() == {'x64': 'win-amd64', 'arm64': 'win-arm64'}[args.arch or ''],
                'Python must be native to the matrix architecture, not emulated')
    root, output = args.repo.resolve(), args.output.resolve()
    require(not output.is_relative_to(root), 'Evidence must be outside source checkout')
    output.mkdir(parents=True, exist_ok=False)
    attrs, hashes = source_guard(root, args.subject)
    nodes = collect(root, sandbox(output / 'collection-sandbox', root))
    metadata = {'subject': args.subject, 'hashes': hashes, 'nodes': nodes, 'collected': len(nodes),
                'unique': len(set(nodes)), 'workers': 2, 'retries': 0, 'effective_file_budget_seconds': 300, 'architecture': args.arch,
                'python': sys.version, 'executable': sys.executable, 'platform': platform.platform(),
                'controller_sha256': sha(Path(__file__).read_bytes()), 'validation_only': args.list,
                'full_file_CI_gate': 'Canonical whole-file -j1, two simultaneous files, 300s, no retries/skips'}
    save(output / 'metadata.json', metadata)
    if args.list:
        print(json.dumps(metadata, indent=2))
        return 0
    summary = {'subject': args.subject, 'accepted': False}
    try:
        cache = root / 'test_durations.json'
        require(not cache.exists(), 'Inherited duration cache is forbidden; use a fresh checkout')
        def whole_file(name):
            expected = [n for n in nodes if n.startswith(name + '::')]
            return run_file(root, output / Path(name).stem, name, expected)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(whole_file, ALL_FILES))
        summary['files'] = results
        # Always attempt the real challenge after the independent whole-file runs.
        summary['mutation'] = mutation(root, output, args.subject, attrs)
        summary['totals'] = aggregate(results, nodes)
        require(all(r['accepted'] for r in results), 'Not every canonical whole-file run passed')
        require(summary['mutation']['accepted'], 'Mutation did not produce exact timestamp RED')
        summary['accepted'] = True
    except Exception as exc:
        summary['error'] = repr(exc)
    finally:
        try:
            _, final = source_guard(root, args.subject)
            require(final == hashes, 'Source hashes changed during execution')
            summary['source_unchanged'] = True
        except Exception as exc:
            summary['source_unchanged'] = False
            summary['accepted'] = False
            summary['source_error'] = repr(exc)
        save(output / 'summary.json', summary)
    print(json.dumps(summary, indent=2))
    return int(not summary['accepted'])


if __name__ == '__main__':
    raise SystemExit(main())
