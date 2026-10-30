"""Portable orchestration fixtures ONLY, never native permission evidence."""
import unittest

import native_controls as controls


class FakeAPI:
    def __init__(self):
        self.events = []
        self.restricted = False

    def no_token(self):
        self.events.append('no-token')
        return 1008

    def enter(self):
        self.events.append('enter')
        self.restricted = True
        return True, 0

    def probe(self, pid):
        self.events.append(('probe', pid))
        return False, 5

    def revert(self):
        self.events.append('revert')
        self.restricted = False
        return True

    def failfast(self, code):
        raise AssertionError('unexpected failfast')


class OwnedDenialTests(unittest.TestCase):
    def fixture(self):
        api = FakeAPI()
        error = OSError('portable fixture only')
        error.winerror = 5
        def query(pid):
            api.events.append(('query', pid, api.restricted))
            if api.restricted:
                handle = None  # noqa: F841 - traceback local is part of the contract.
                raise error
            return '1700000000.001'
        def live():
            self.assertFalse(api.restricted, 'child polling while restricted')
            api.events.append('live')
            return True
        reports = []
        def report(event, **fields):
            self.assertFalse(api.restricted, 'reporting while restricted')
            reports.append((event, fields))
        return api, query, query.__code__.co_firstlineno + 4, live, report, reports

    def run_control(self, fixture):
        function = getattr(controls, 'owned_denial_control', None)
        self.assertTrue(callable(function), 'missing owned-thread denial orchestration')
        api, query, line, live, report, _ = fixture
        return function(12, query, line, live, api, report,
                        dict(helper_source_sha256='portable-fixture-not-native'))

    def test_owned_child_denial_reverts_and_recovers_before_pass(self):
        fixture = self.fixture()
        self.assertTrue(self.run_control(fixture))
        api, _, _, _, _, reports = fixture
        self.assertEqual(api.events, ['no-token', 'live', ('query', 12, False), 'enter',
                                     ('probe', 12), ('query', 12, True), 'revert',
                                     'no-token', 'live', ('query', 12, False), 'live'])
        self.assertEqual([event for event, _ in reports], ['control_pass'])
        fields = reports[0][1]
        self.assertEqual(fields['native_winerror'], 5)
        self.assertEqual(fields['candidate_winerror'], 5)
        self.assertEqual(fields['owner_pid'], 12)
        self.assertEqual(fields['fixture'], 'owned-child-current-thread-anonymous')
        self.assertTrue(fields['recovered'])
        self.assertEqual(fields['identity_before'], '1700000000.001')
        self.assertEqual(fields['identity_after'], fields['identity_before'])
        self.assertEqual(fields['thread_token_before'], 1008)
        self.assertEqual(fields['thread_token_after'], 1008)
        self.assertEqual(fields['query_access'], 0x1000)
        self.assertEqual(fields['failure_line'], fixture[2])
        self.assertEqual(fields['failure_function'], fixture[1].__name__)
        self.assertEqual(fields['failure_path'], fixture[1].__code__.co_filename)

    def test_unavailable_native_precondition_is_blocked_after_recovery(self):
        for scenario in ('entry', 'granted', 'wrong-error'):
            with self.subTest(scenario=scenario):
                fixture = self.fixture()
                api, _, _, _, _, reports = fixture
                if scenario == 'entry':
                    def entry():
                        api.events.append('enter')
                        return False, 1314
                    api.enter = entry
                else:
                    api.probe = lambda pid: (scenario == 'granted', 87)
                self.assertFalse(self.run_control(fixture))
                self.assertIn('revert', api.events)
                self.assertEqual([event for event, _ in reports], ['control_blocked'])
                self.assertEqual(sum(event == ('query', 12, True) for event in api.events), 0)
                self.assertEqual(api.events[-1], 'live')

    def test_helper_error_requires_exact_failed_openprocess_traceback(self):
        for scenario in ('line', 'loader', 'handle', 'pid'):
            with self.subTest(scenario=scenario):
                fixture = list(self.fixture())
                api, original, line, _, _, reports = fixture
                if scenario == 'line':
                    fixture[2] = line + 1
                else:
                    error = OSError('portable fixture only')
                    error.winerror = 5
                    def loader():
                        raise error
                    def wrong(pid):
                        if not api.restricted:
                            return original(pid)
                        handle = 99 if scenario == 'handle' else None  # noqa: F841 - traceback contract.
                        if scenario == 'pid':
                            pid = 13
                        if scenario == 'loader':
                            loader()
                        raise error
                    fixture[1] = wrong
                    fixture[2] = wrong.__code__.co_firstlineno + 8
                with self.assertRaisesRegex(AssertionError, 'failed OpenProcess branch'):
                    self.run_control(fixture)
                if scenario in ('handle', 'pid'):
                    frame = error.__traceback__
                    while frame.tb_next is not None:
                        frame = frame.tb_next
                    self.assertIs(frame.tb_frame.f_code, wrong.__code__)
                    self.assertEqual(frame.tb_lineno, fixture[2])
                self.assertEqual(reports, [])
                self.assertIn('revert', api.events)
                self.assertFalse(api.restricted)

    def test_revert_failure_failfast_precedes_all_postfailure_io(self):
        class Stopped(BaseException):
            pass
        for throws in (False, True):
            with self.subTest(throws=throws):
                fixture = self.fixture()
                api, _, _, _, _, reports = fixture
                def revert():
                    api.events.append('revert-failed')
                    if throws:
                        raise OSError('portable revert failure')
                    return False
                def failfast(code):
                    api.events.append(('failfast', code))
                    raise Stopped()
                api.revert = revert
                api.failfast = failfast
                with self.assertRaises(Stopped):
                    self.run_control(fixture)
                self.assertEqual(api.events[-2:], ['revert-failed', ('failfast', 86)])
                self.assertEqual(reports, [])
                self.assertTrue(api.restricted)

    def test_unexpected_restricted_exception_still_checks_recovery_before_reraise(self):
        for stage in ('enter', 'probe', 'helper'):
            with self.subTest(stage=stage):
                fixture = list(self.fixture())
                api, original, _, _, _, reports = fixture
                def broken(*args):
                    raise RuntimeError('portable unexpected failure')
                if stage == 'enter':
                    api.enter = broken
                elif stage == 'probe':
                    api.probe = broken
                else:
                    def query(pid):
                        if api.restricted:
                            broken()
                        return original(pid)
                    fixture[1] = query
                with self.assertRaisesRegex(RuntimeError, 'portable unexpected failure'):
                    self.run_control(fixture)
                self.assertFalse(api.restricted)
                self.assertIn('revert', api.events)
                self.assertEqual(api.events[-4:], ['no-token', 'live', ('query', 12, False), 'live'])
                self.assertEqual(reports, [])

    def test_post_revert_token_verification_failure_also_failfast_before_io(self):
        class Stopped(BaseException):
            pass
        fixture = self.fixture()
        api, _, _, _, _, reports = fixture
        errors = iter((1008, 1347))
        api.no_token = lambda: next(errors)
        def failfast(code):
            raise Stopped()
        api.failfast = failfast
        with self.assertRaises(Stopped):
            self.run_control(fixture)
        self.assertEqual(api.events[-1], 'revert')
        self.assertEqual(reports, [])

    def test_existing_negative_contracts_never_emit_pass(self):
        for scenario in ('pre-token', 'baseline-exit', 'denial-exit', 'recovery-exit',
                         'changed-identity', 'helper-passed', 'wrong-helper-error'):
            with self.subTest(scenario=scenario):
                fixture = list(self.fixture())
                api, original, _, _, _, reports = fixture
                if scenario == 'pre-token':
                    api.no_token = lambda: 0
                elif scenario.endswith('exit'):
                    lives = iter({'baseline-exit': [False], 'denial-exit': [True, False],
                                  'recovery-exit': [True, True, False]}[scenario])
                    fixture[3] = lambda: next(lives)
                elif scenario == 'changed-identity':
                    def changed(pid):
                        if 'revert' in api.events:
                            return 'changed'
                        return original(pid)
                    fixture[1] = changed
                elif scenario == 'helper-passed':
                    fixture[1] = lambda pid: '1700000000.001'
                else:
                    def wrong(pid):
                        if api.restricted:
                            raise OSError('not winerror 5')
                        return original(pid)
                    fixture[1] = wrong
                with self.assertRaises(AssertionError):
                    self.run_control(fixture)
                self.assertEqual(reports, [])
                if scenario in ('pre-token', 'baseline-exit'):
                    self.assertNotIn('enter', api.events)
                    self.assertNotIn('revert', api.events)
                else:
                    self.assertIn('revert', api.events)

    def test_actual_archived_helper_is_loaded_and_failed_open_branch_bound(self):
        from pathlib import Path
        import tempfile
        import controller
        prepare = getattr(controls, 'prepare_owned_source', None)
        self.assertTrue(callable(prepare), 'missing exact-source permission preparation')
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'source'
            controller.archive_subject(Path(__file__).resolve().parents[3],
                                       '81f29fbf2ac018f41ec4325a229d2ee3e913222b', repo)
            query, line, receipt = prepare(repo)
            self.assertEqual(receipt['helper_source_sha256'],
                             '9d19bbd5320e6dcdafa2b6fdd96c54401848e72c1e92ee5459ace3a18e972331')
            self.assertEqual(Path(query.__code__.co_filename).resolve(), (repo / controller.HELPER).resolve())
            self.assertEqual((repo / controller.HELPER).read_text().splitlines()[line - 1].strip(),
                             'raise ctypes.WinError(ctypes.get_last_error())')
            self.assertEqual(receipt['denied_raise_line'], line)
            (repo / controller.HELPER).write_text('raise RuntimeError("must reject before import")\n')
            with self.assertRaisesRegex(AssertionError, 'actual imported source SHA256'):
                prepare(repo)

    def test_native_bindings_are_preloaded_typed_and_capture_errors_before_close(self):
        import ctypes
        from ctypes import wintypes as w
        from types import SimpleNamespace
        binding = getattr(controls, 'OwnedThreadAPI', None)
        self.assertTrue(callable(binding), 'missing pointer-correct owned-thread API bindings')
        events = []
        class Function:
            def __init__(self, name, result):
                self.name, self.result = name, result
            def __call__(self, *args):
                events.append((self.name, args))
                return self.result
        k = SimpleNamespace(GetCurrentThread=Function('thread', -2),
                            OpenProcess=Function('open', None), CloseHandle=Function('close', True))
        a = SimpleNamespace(OpenThreadToken=Function('token', False),
                            ImpersonateAnonymousToken=Function('anonymous', True),
                            RevertToSelf=Function('revert', True))
        def loader(name, **kwargs):
            events.append(('load', name, kwargs))
            return {'kernel32': k, 'advapi32': a}[name]
        def last_error():
            events.append('error')
            return 5
        api = binding(loader, last_error, lambda code: None)
        self.assertEqual(events[:2], [('load', 'kernel32', {'use_last_error': True}),
                                     ('load', 'advapi32', {'use_last_error': True})])
        for function, args, result in (
            (k.GetCurrentThread, (), w.HANDLE),
            (k.OpenProcess, (w.DWORD, w.BOOL, w.DWORD), w.HANDLE),
            (k.CloseHandle, (w.HANDLE,), w.BOOL),
            (a.OpenThreadToken, (w.HANDLE, w.DWORD, w.BOOL, ctypes.POINTER(w.HANDLE)), w.BOOL),
            (a.ImpersonateAnonymousToken, (w.HANDLE,), w.BOOL),
            (a.RevertToSelf, (), w.BOOL),
        ):
            self.assertEqual(function.argtypes, args)
            self.assertIs(function.restype, result)
        events.clear()
        self.assertEqual(api.no_token(), 5)
        self.assertEqual(events[0][0], 'token')
        self.assertEqual(events[0][1][:3], (-2, 8, True))
        self.assertEqual(events[1], 'error')
        events.clear()
        self.assertEqual(api.enter(), (True, 0))
        self.assertEqual(events, [('anonymous', (-2,))])
        events.clear()
        self.assertEqual(api.probe(12), (False, 5))
        self.assertEqual(events, [('open', (0x1000, False, 12)), 'error'])
        k.OpenProcess.result = 123
        events.clear()
        self.assertEqual(api.probe(12), (True, 0))
        self.assertEqual(events, [('open', (0x1000, False, 12)), ('close', (123,))])
        self.assertTrue(api.revert())

    def test_controller_passes_existing_pinned_archive_to_permission_control(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        import controller
        commands = []
        def execute(command, repo, folder, env):
            commands.append(command)
            return dict(outer_timeout=False, returncode=3)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(controller, 'execute', execute), patch.object(controller, 'records', return_value=[]):
                controller.run_native_diagnostics(root / 'original', root / 'green', root / 'out')
            equality = next(command for command in commands if any('native_controls.py' in arg for arg in command))
            self.assertIn('--denial-repo', equality)
            self.assertEqual(equality[equality.index('--denial-repo') + 1], str(root / 'green'))
            self.assertEqual(equality[equality.index('--repo') + 1], str(root / 'original'))

    def test_main_uses_owned_fixture_not_foreign_pid_scan_or_timing_wrapper(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(controls.main))
        calls = [ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)]
        self.assertIn('prepare_owned_source', calls)
        self.assertIn('OwnedThreadAPI', calls)
        self.assertIn('owned_denial_control', calls)
        self.assertNotIn('inaccessible_control', calls)
        self.assertNotIn('psutil.pids', calls)
        owned = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and
                     ast.unparse(node.func) == 'owned_denial_control')
        self.assertEqual(ast.unparse(owned.args[1]), 'denied_query')
        prepare = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign) and
                       isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == 'prepare_owned_source')
        self.assertEqual(ast.unparse(prepare.targets[0]), '(denied_query, denied_line, denied_receipt)')

    def test_controller_requires_exact_source_denial_recovery_and_nonblocked_exit(self):
        from pathlib import Path
        import controller
        classify = getattr(controller, 'classify_owned_denial', None)
        self.assertTrue(callable(classify), 'missing source-bound denial classifier')
        repo = Path('/portable/archived')
        path = str(repo / controller.HELPER)
        record = dict(event='control_pass', control='inaccessible-pid', owner_pid=12,
                      fixture='owned-child-current-thread-anonymous',
                      native_winerror=5, candidate_winerror=5, recovered=True,
                      thread_token_before=1008, thread_token_after=1008, query_access=0x1000,
                      identity_before='1700000000.001', identity_after='1700000000.001',
                      helper_source_sha256='9d19bbd5320e6dcdafa2b6fdd96c54401848e72c1e92ee5459ace3a18e972331',
                      helper_module='marker_owned_denial_source', helper_path=path,
                      failure_path=path, failure_line=84, denied_raise_line=84,
                      failure_function='_creation_time')
        complete = dict(event='controls_complete', blocked_controls=[])
        self.assertTrue(classify(0, [record, complete], repo))
        for key, value in dict(native_winerror=87, candidate_winerror=87, recovered=False,
                               thread_token_before=0, thread_token_after=1347, query_access=0x400,
                               identity_after='changed', identity_before='', owner_pid=0,
                               helper_source_sha256='wrong', helper_module='candidate',
                               helper_path='/foreign.py', failure_path='/loader.py',
                               failure_line=85, failure_function='WinDLL',
                               fixture='foreign-pid-scan').items():
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                classify(0, [dict(record, **{key: value}), complete], repo)
        for code, data in ((3, [record, complete]), (0, [complete]),
                           (0, [record, record, complete]),
                           (0, [record, dict(complete, blocked_controls=['inaccessible-pid'])]),
                           (0, [record, complete, dict(event='control_blocked')])):
            with self.assertRaises(RuntimeError):
                classify(code, data, repo)

    def test_real_owned_process_failfast_does_not_flush_or_report(self):
        from pathlib import Path
        import subprocess
        import sys
        for mode in ('revert', 'token', 'fallback'):
            with self.subTest(mode=mode):
                code = '''import os
from test_owned_denial import OwnedDenialTests
case = OwnedDenialTests()
fixture = case.fixture()
api = fixture[0]
print('buffered output must not be flushed')
'''
                if mode == 'token':
                    code += 'errors = iter((1008, 1347))\napi.no_token = lambda: next(errors)\n'
                else:
                    code += 'api.revert = lambda: False\n'
                code += ('api.failfast = lambda code: None\n' if mode == 'fallback' else
                         'api.failfast = os._exit\n')
                code += 'case.run_control(fixture)\nraise AssertionError("continued after failfast")\n'
                result = subprocess.run([sys.executable, '-c', code],
                                        cwd=Path(controls.__file__).parent,
                                        capture_output=True, text=True, timeout=10, check=False)
                self.assertEqual(result.returncode, 86, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
