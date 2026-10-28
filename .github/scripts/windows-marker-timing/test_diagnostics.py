"""Portable logic contracts only: no OS spoofing or native evidence."""
import unittest

import native_controls as controls


class DiagnosticTests(unittest.TestCase):
    def test_selected_bare_pytest_module_globals_are_actually_wrapped(self):
        from pathlib import Path
        from types import SimpleNamespace
        from typing import Any
        import tempfile
        import marker_timing_plugin as plugin
        bind = getattr(plugin, 'bind_selected_helpers', None)
        self.assertTrue(callable(bind), 'missing actual selected-function global binding')
        assert callable(bind)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relative = 'tests/scripts/desktop_update/test_desktop_update_windows_marker_bodies.py'
            namespace: dict[str, Any] = {'__file__': str(root / relative), '__name__': 'test_desktop_update_windows_marker_bodies'}
            calls = []
            original = lambda: calls.append('original')
            wrapped = lambda: (calls.append('wrapped'), original())
            namespace.update(_creation_time=original, _run=original)
            exec('def selected():\n _creation_time()\n _run()\n', namespace)
            item = SimpleNamespace(nodeid=relative + '::selected', obj=namespace['selected'])
            aliases = bind([item], root, {'_creation_time': original, '_run': original},
                           {'_creation_time': wrapped, '_run': wrapped})
            namespace['selected']()
            self.assertEqual(calls, ['wrapped', 'original', 'wrapped', 'original'])
            expected = relative[:-3].replace('/', '.')
            self.assertEqual(set(aliases), {expected + '._creation_time', expected + '._run'})
            namespace['__file__'] = str(root / 'foreign.py')
            with self.assertRaises(RuntimeError):
                bind([item], root, {'_creation_time': original, '_run': original},
                     {'_creation_time': wrapped, '_run': wrapped})

    def test_windows_popen_audit_uses_actual_command_when_executable_is_none(self):
        import native_no_shell as no_shell
        import controller
        extract = getattr(no_shell, 'audited_executable', None)
        self.assertTrue(callable(extract), 'missing Windows Popen audit extraction')
        assert callable(extract)
        for values, expected in (
            ((None, 'powershell -NoProfile -Command example', None, None), 'powershell'),
            ((None, '"C:\\Program Files\\PowerShell\\powershell.exe" -NoProfile', None, None),
             'C:\\Program Files\\PowerShell\\powershell.exe'),
            ((None, ['powershell', '-NoProfile'], None, None), 'powershell'),
            (('powershell.exe', 'ignored', None, None), 'powershell.exe'),
        ):
            actual = extract(values)
            self.assertEqual(actual, expected)
            record = dict(event='no_shell_observed', implementation='original',
                          identity='1700000000.001', helper_source_sha256=controller.HELPER_SHA,
                          subprocess_executables=[actual])
            self.assertTrue(controller.classify_red(1, [record]))
        wrong = dict(event='no_shell_observed', implementation='original',
                     identity='1700000000.001', helper_source_sha256=controller.HELPER_SHA,
                     subprocess_executables=[extract((None, 'python script.py', None, None))])
        with self.assertRaises(RuntimeError):
            controller.classify_red(1, [wrong])

    def test_held_dead_query_requires_exact_exited_exception(self):
        check = getattr(controls, 'require_dead_query', None)
        self.assertTrue(callable(check), 'missing strict held-dead classifier')
        def exited(pid):
            raise ProcessLookupError(f'process {pid} has exited')
        check(exited, 12)
        class Derived(ProcessLookupError):
            pass
        for error in (OSError('access denied'), ProcessLookupError('no creation identity'),
                      Derived('has exited')):
            def wrong(pid, error=error):
                raise error
            with self.assertRaises(AssertionError):
                check(wrong, 12)
        with self.assertRaises(AssertionError):
            check(lambda pid: '1000.000', 12)


    def test_accessible_pid4_is_blocked_not_a_pass_or_exception(self):
        check = getattr(controls, 'inaccessible_control', None)
        self.assertTrue(callable(check), 'missing non-truncating inaccessible control')
        events, closed, queries = [], [], []
        report = lambda event, **fields: events.append((event, fields))
        self.assertFalse(check(lambda: 123, closed.append, lambda: 0,
                               queries.append, report))
        self.assertEqual(closed, [123])
        self.assertEqual(queries, [])
        self.assertEqual(events[0][0], 'control_blocked')
        self.assertIn('grants limited access', events[0][1]['reason'])
        self.assertFalse(any(event == 'control_pass' for event, _ in events))
        events.clear()
        def denied(pid):
            error = OSError('denied')
            error.winerror = 5
            raise error
        self.assertTrue(check(lambda: None, closed.append, lambda: 5, denied, report))
        self.assertEqual(events[0][0], 'control_pass')
        with self.assertRaises(AssertionError):
            check(lambda: None, closed.append, lambda: 87, denied, report)
        with self.assertRaises(AssertionError):
            check(lambda: None, closed.append, lambda: 5, lambda pid: 'identity', report)


    def test_closure_measures_success_and_both_held_dead_paths(self):
        from types import SimpleNamespace
        check = getattr(controls, 'check_handle_closure', None)
        self.assertTrue(callable(check), 'missing success/exception closure measurements')
        calls, events = [], []
        held = [(SimpleNamespace(pid=20, returncode=0), 0),
                (SimpleNamespace(pid=21, returncode=259), 259)]
        def query(pid):
            calls.append(pid)
            if pid != 10:
                raise ProcessLookupError(f'process {pid} has exited')
            return '1700000000.000'
        report = lambda event, **fields: events.append((event, fields))
        check(query, 10, '1700000000.000', held, lambda: 42, report)
        self.assertEqual([calls.count(pid) for pid in (10, 20, 21)], [64, 64, 64])
        self.assertEqual(events[0][1]['dead_queries_by_exit_code'], {'0': 64, '259': 64})
        self.assertEqual(events[0][1]['before'], events[0][1]['after'])
        counts = iter((42, 43))
        with self.assertRaises(AssertionError):
            check(query, 10, '1700000000.000', held, lambda: next(counts), report)
        held[1][0].returncode = 0
        with self.assertRaises(AssertionError):
            check(query, 10, '1700000000.000', held, lambda: 42, report)


    def test_optimized_interpreters_fail_closed_before_native_controls(self):
        import json
        import os
        from pathlib import Path
        import subprocess
        import sys
        import tempfile
        for script in ('native_controls.py', 'native_no_shell.py'):
            for flag, setting in ((['-O'], '0'), ([], '1')):
                with tempfile.TemporaryDirectory() as tmp:
                    args = [sys.executable, *flag, str(Path(controls.__file__).with_name(script)),
                            '--repo', tmp, '--out', tmp]
                    if script == 'native_no_shell.py':
                        args += ['--implementation', 'original']
                    env = dict(os.environ, PYTHONOPTIMIZE=setting)
                    proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10, check=False)
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    records = [json.loads(line) for p in Path(tmp).glob('timing-*.jsonl')
                               for line in p.read_text().splitlines()]
                    self.assertEqual(records[0]['reason'], 'assertion controls disabled by optimization')


    def test_no_shell_binds_actual_import_and_exact_source_hash(self):
        import hashlib
        import importlib.util
        from pathlib import Path
        import tempfile
        import native_no_shell as no_shell
        receipt = getattr(no_shell, 'source_receipt', None)
        validate = getattr(no_shell, 'require_source', None)
        self.assertTrue(callable(receipt) and callable(validate), 'missing imported-source binding')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'tests/scripts/desktop_update/test_desktop_update_windows_marker.py'
            path.parent.mkdir(parents=True)
            path.write_text('def _creation_time(pid):\n    return "fixture-only"\n')
            spec = importlib.util.spec_from_file_location('portable_source_fixture', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            data = receipt(module, root)
            self.assertEqual(data['helper_source_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(data['helper_module'], module.__name__)
            with self.assertRaises(AssertionError):
                validate('original', data)
            with self.assertRaises(AssertionError):
                validate('patched-source', data)
            with self.assertRaises(AssertionError):
                validate('candidate', data)
            for label, sha in [('original', '2cb44bc565731abd75eea33376eb250065ce97471aee7b5acf83274145e6c36f'),
                               ('patched-source', '9d19bbd5320e6dcdafa2b6fdd96c54401848e72c1e92ee5459ace3a18e972331')]:
                validate(label, dict(data, helper_source_sha256=sha))  # classifier fixture only
            module._creation_time = controls.require_dead_query
            with self.assertRaises(AssertionError):
                receipt(module, root)


    def test_blocked_control_still_runs_closure_and_finishes_nonzero(self):
        finish = getattr(controls, 'finish_controls', None)
        self.assertTrue(callable(finish), 'missing tested completion/blocked-status contract')
        for available, expected in ((False, 3), (True, 0)):
            events = []
            def closure():
                events.append(('closure', {}))
            report = lambda event, **fields: events.append((event, fields))
            self.assertEqual(finish(available, closure, report), expected)
            self.assertEqual([event for event, _ in events], ['closure', 'controls_complete'])
            self.assertEqual(events[-1][1]['blocked_controls'], [] if available else ['inaccessible-pid'])
        def failed_closure():
            raise AssertionError('leak')
        with self.assertRaises(AssertionError):
            finish(False, failed_closure, report)


if __name__ == '__main__':
    unittest.main()
