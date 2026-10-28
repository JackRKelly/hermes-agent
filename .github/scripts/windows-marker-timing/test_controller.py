"""Portable controller tests; never a native Windows acceptance claim."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import controller as c


class ControllerTests(unittest.TestCase):
    def test_red_requires_identity_and_actual_powershell_audit(self):
        record = dict(event='no_shell_observed', implementation='original', helper_source_sha256=c.HELPER_SHA,
                      identity='1700000000.001', subprocess_executables=['powershell.exe'])
        self.assertTrue(c.classify_red(1, [record]))
        for code, records in [(0, [record]), (2, [record]), (1, []),
                              (1, [dict(record, identity='')]),
                              (1, [dict(record, helper_source_sha256='wrong')]),
                              (1, [{k: v for k, v in record.items() if k != 'helper_source_sha256'}]),
                              (1, [dict(record, subprocess_executables=[])]),
                              (1, [dict(record, subprocess_executables=['python.exe'])])]:
            with self.assertRaises(RuntimeError):
                c.classify_red(code, records)

    def test_green_requires_actual_source_identity_no_popen_and_complete_exit_zero(self):
        sha = '9d19bbd5320e6dcdafa2b6fdd96c54401848e72c1e92ee5459ace3a18e972331'
        receipt = dict(implementation='patched-source', helper_source_sha256=sha,
                       helper_module='tests.scripts.desktop_update.test_desktop_update_windows_marker',
                       helper_path='/portable/archived/' + c.HELPER)
        data = [dict(event='no_shell_source', **receipt),
                dict(event='no_shell_start', **receipt),
                dict(event='no_shell_observed', identity='1700000000.001',
                     subprocess_executables=[], **receipt),
                dict(event='no_shell_complete', **receipt)]
        self.assertTrue(c.classify_green(0, data))
        self.assertTrue(c.classify_green(0, data, Path('/portable/archived')))
        with self.assertRaises(RuntimeError):
            c.classify_green(0, data, Path('/foreign/source'))
        for code, records in [(1, data), (3, data), (0, data[:-1]),
                              (0, [dict(r, helper_source_sha256='wrong') for r in data]),
                              (0, [dict(r, implementation='candidate') for r in data]),
                              (0, [dict(r, identity='') if r['event'] == 'no_shell_observed' else r for r in data]),
                              (0, [dict(r, subprocess_executables=['python.exe'])
                                   if r['event'] == 'no_shell_observed' else r for r in data])]:
            with self.assertRaises(RuntimeError):
                c.classify_green(code, records)

    def test_archive_requires_exact_commit_and_preserves_real_git_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Path(c.__file__).resolve().parents[3]
            sha = c.git(repo, 'rev-parse', 'HEAD')
            expected = subprocess.check_output(['git', 'show', sha + ':' + c.HELPER], cwd=repo)
            c.archive_subject(repo, sha, root / 'archive')
            self.assertEqual((root / 'archive' / c.HELPER).read_bytes(), expected)
            for invalid in ('HEAD', sha[:39], sha + ';bad', 'UNBOUND_FINAL_SUBJECT'):
                with self.assertRaises(RuntimeError):
                    c.archive_subject(repo, invalid, root / 'bad')
            with self.assertRaises(RuntimeError):
                c.archive_subject(repo, sha, repo / 'nested')

    def test_blocked_equality_does_not_truncate_actual_source_green(self):
        from unittest.mock import patch
        calls = []
        def execute(command, repo, folder, env):
            calls.append(folder.name)
            return dict(outer_timeout=False, returncode=3 if folder.name == 'native-equality' else 0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(c, 'execute', execute), patch.object(c, 'records', return_value=[]), \
                 patch.object(c, 'classify_red', return_value=True), \
                 patch.object(c, 'classify_green', return_value=True):
                failures = c.run_native_diagnostics(root / 'original', root / 'green', root / 'out')
            self.assertEqual(calls, ['native-red', 'native-equality', 'patched-source-green'])
            self.assertEqual(len(failures), 1)
            self.assertIn('native-equality', failures[0])

    def test_process_success_failure_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, script, cap, code, timed_out in [
                ('ok', 'print(123)', 5, 0, False),
                ('fail', 'raise SystemExit(7)', 5, 7, False),
                ('timeout', 'import time; time.sleep(20)', 1, None, True),
            ]:
                result = c.execute([sys.executable, '-c', script], Path.cwd(),
                                   Path(tmp) / name, dict(c.clean_env(Path(tmp) / name)), budget=cap)
                self.assertEqual(result['outer_timeout'], timed_out)
                if code is not None:
                    self.assertEqual(result['returncode'], code)
                self.assertLess(result['elapsed_seconds'], cap + 1)
                self.assertTrue((Path(tmp) / name / 'process.json').is_file())

    def test_cleanup_stops_real_owned_descendant_after_parent_success(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            marker = folder / 'survived.txt'
            child = ('import time; from pathlib import Path; time.sleep(1); '
                     'Path(' + repr(str(marker)) + ').write_text("escaped")')
            parent = 'import subprocess,sys; subprocess.Popen([sys.executable,"-c",' + repr(child) + '])'
            result = c.execute([sys.executable, '-c', parent], Path.cwd(),
                               folder / 'owned', c.clean_env(folder / 'sandbox'), budget=5)
            self.assertEqual(result['returncode'], 0)
            time.sleep(1.25)
            self.assertFalse(marker.exists(), 'Owned descendant escaped successful-command cleanup')

    def test_source_guard_failure_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run([sys.executable, str(Path(c.__file__)), '--repo', tmp,
                                   '--out', str(Path(tmp) / 'out'), '--guard-only'],
                                  capture_output=True, text=True, check=False)
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(json.loads((Path(tmp) / 'out' / 'summary.json').read_text())['accepted'])

    def test_final_source_guard_failure_cannot_return_success(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            args = ['controller', '--repo', tmp, '--out', str(Path(tmp) / 'out'), '--guard-only']
            with patch.object(sys, 'argv', args), patch.object(c, 'source_guard',
                    side_effect=[{}, RuntimeError('post-run source mutation')]):
                with self.assertRaises(SystemExit) as error:
                    c.main()
            self.assertEqual(error.exception.code, 1)
            self.assertFalse(json.loads((Path(tmp) / 'out/summary.json').read_text())['accepted'])

    def test_timing_classifier_rejects_skip_wrong_node_and_partial_helpers(self):
        # Synthetic classifier fixtures only, not native evidence.
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            xml = ('<testsuite tests="1"><testcase classname="' + c.BODY[:-3].replace('/', '.') +
                   '" name="' + c.CASE + '"/></testsuite>')
            (folder / 'junit.xml').write_text(xml)
            data = [dict(event='collection', selected_nodeids=[c.BODY + '::' + c.CASE],
                         helper_source_sha256=c.HELPER_SHA, platform='win32', machine='AMD64',
                         wrapped_aliases=[c.BODY[:-3].replace('/', '.') + '.' + helper
                                          for helper in ('_creation_time', '_run')])]
            for helper in ('_creation_time', '_run'):
                data.extend([dict(event='helper_start', invocation=helper, helper=helper),
                             dict(event='helper_end', invocation=helper, helper=helper)])
            data.extend(dict(event='phase_end', phase=p, outcome='passed')
                        for p in ('setup', 'call', 'teardown'))
            def put(records):
                (folder / 'timing-unit.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records))
            put(data)
            c.classify_timing(0, folder)
            for modified in (data[:-1], [r for r in data if r.get('invocation') != '_run'],
                             [dict(data[0], wrapped_aliases=[]) , *data[1:]],
                             [dict(data[0], wrapped_aliases=data[0]['wrapped_aliases'][:1]), *data[1:]],
                             [dict(data[0], wrapped_aliases=['another_module._creation_time',
                                                            'another_module._run']), *data[1:]],
                             [{k: v for k, v in data[0].items() if k != 'wrapped_aliases'}, *data[1:]]):
                put(modified)
                with self.assertRaises(RuntimeError):
                    c.classify_timing(0, folder)
            put(data)
            (folder / 'junit.xml').write_text(xml.replace('/>', '><skipped/></testcase>'))
            with self.assertRaises(RuntimeError):
                c.classify_timing(0, folder)
            (folder / 'junit.xml').write_text(xml.replace(c.CASE, 'wrong-test'))
            with self.assertRaises(RuntimeError):
                c.classify_timing(0, folder)
            (folder / 'junit.xml').write_text(xml)
            with self.assertRaises(RuntimeError):
                c.classify_timing(7, folder)

    def test_sandbox_does_not_forward_runtime_or_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = c.clean_env(Path(tmp))
        self.assertNotIn('HERMES_RUNTIME_DIR', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('PYTHONPATH', env)
        self.assertEqual(env['HERMES_TEST_FILE_RETRIES'], '0')


    def test_git_bash_selection_rejects_missing_relative_and_system32(self):
        select = getattr(c, 'resolve_native_bash', None)
        self.assertTrue(callable(select), 'missing absolute Git Bash selection')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bash = root / 'Git/bin/bash.exe'
            bash.parent.mkdir(parents=True)
            bash.write_bytes(b'portable path fixture; never executed')
            self.assertEqual(select(root), bash.resolve())
            with self.assertRaises(RuntimeError):
                select(Path('relative'))
            bash.unlink()
            system = root / 'System32/bash.exe'
            system.parent.mkdir()
            system.write_bytes(b'not Git Bash; never executed')
            with self.assertRaises(RuntimeError):
                select(root)
            bash.symlink_to(system)
            with self.assertRaises(RuntimeError):
                select(root)
            bash.unlink()
            (root / 'Git/bin').rmdir()
            (root / 'Git').rmdir()
            (root / 'Git').symlink_to(system.parent, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                select(root)


if __name__ == '__main__':
    unittest.main()
