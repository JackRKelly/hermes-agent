"""Controlled sentinel owner -> actual managed child; no provider or live admission."""
import asyncio
import io
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest

import pytest



class ProtectedToolAdapterTests(unittest.TestCase):
    def test_production_adapter_is_denied_before_profile_environment(self):
        from gateway.session_managed_worker import _spawn_registered_worker
        authority = SimpleNamespace()
        ref = SimpleNamespace(session_id='sentinel-refused')
        with self.assertRaisesRegex(RuntimeError, 'admission disabled'):
            asyncio.run(_spawn_registered_worker(authority, ref,
                {'generation': 1, 'protected_tools_v1': {'version': 1, 'adapter': 'readonly-v1'}}))
        self.assertEqual(authority._managed_spawning, {})

    @pytest.mark.platforms("posix")
    def test_native_seam_runs_fixed_tools_without_profile_bootstrap(self):
        from gateway.session_managed_worker import _spawn_registered_worker
        from gateway.session_protected_tools import SentinelLaunch, ToolProxy
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'allowed.txt').write_text('SENTINEL_ONLY\n', encoding='utf-8')
            fd = os.open(root / 'allowed.txt', os.O_RDONLY)
            os.set_inheritable(fd, True)
            import fcntl
            import subprocess
            high = fcntl.fcntl(fd, fcntl.F_DUPFD, 400)
            os.set_inheritable(high, True)
            directory_fd = os.open(root, os.O_RDONLY)
            os.set_inheritable(directory_fd, True)
            control = subprocess.check_output(['/bin/cat', f'/dev/fd/{high}'], pass_fds=(high,))
            self.assertEqual(control, b'SENTINEL_ONLY\n')
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(root / 'listener'))
            listener.listen()
            os.set_inheritable(listener.fileno(), True)
            try:
                async def exercise():
                    authority = SimpleNamespace(pending_stop_causes={})
                    ref = SimpleNamespace(session_id='sentinel-session')
                    row = {'generation': 1}
                    launch = SentinelLaunch(root)
                    worker, process = await _spawn_registered_worker(authority, ref, row, sentinel=launch)
                    try:
                        proxy = ToolProxy(worker, launch)
                        hello = proxy.bind()
                        self.assertEqual(hello['pid'], process.pid)
                        self.assertEqual(set(hello['fds']), {'0', '1', '2'})
                        self.assertNotIn('HERMES_HOME', hello['environment'])
                        self.assertNotIn('SENTINEL_PROVIDER_AUTH', hello['environment'])
                        self.assertLessEqual(set(hello['environment']), {'LANG', 'LC_ALL', '__CF_USER_TEXT_ENCODING'})
                        self.assertEqual(hello['imports'], ['agent', 'agent.jiter_preload', 'agent.protected_tool_worker'])
                        self.assertEqual(proxy.invoke('read_file', {}), '1|SENTINEL_ONLY')
                        self.assertEqual(proxy.last_imports, ['agent', 'agent.jiter_preload', 'agent.protected_tool_worker',
                            'tools', 'tools.file_operations_native'])
                        self.assertEqual(proxy.invoke('terminal', {}), 'SENTINEL_ONLY\n')
                        for name in ('write_file', 'patch', 'delegate_task', 'browser', 'execute_code', 'tool_call', 'mcp'):
                            with self.assertRaises(ValueError):
                                proxy.invoke(name, {})
                        with self.assertRaises(ValueError):
                            proxy.invoke('terminal', {'command': 'anything'})
                        proxy.cancel()
                        with self.assertRaises(ValueError):
                            proxy.invoke('read_file', {})
                    finally:
                        worker.close()
                    self.assertIsNotNone(process.returncode)
                    self.assertEqual(authority._managed_workers, {})
                    self.assertEqual(authority._managed_spawning, {})
                asyncio.run(exercise())
            finally:
                os.close(fd)
                os.close(high)
                os.close(directory_fd)
                listener.close()

    @pytest.mark.platforms("posix")
    def test_fixed_native_read_refuses_symlink_and_oversize(self):
        from agent.protected_tool_worker import read_sentinel_file
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'outside.txt').write_text('NOT_AN_ASSIGNED_SENTINEL', encoding='utf-8')
            (root / 'allowed.txt').symlink_to(root / 'outside.txt')
            with self.assertRaises((ValueError, OSError)):
                read_sentinel_file(root)
            (root / 'allowed.txt').unlink()
            (root / 'allowed.txt').write_bytes(b'x' * 4097)
            with self.assertRaises(ValueError):
                read_sentinel_file(root)

    @pytest.mark.platforms("posix")
    def test_real_child_rejects_replay_unknown_arguments_and_general_rpc(self):
        from gateway.session_managed_worker import _spawn_registered_worker
        from gateway.session_protected_tools import SentinelLaunch, ToolProxy
        frames = [
            {'op': 'call', 'sequence': 2, 'name': 'read_file', 'args': {}},
            {'op': 'call', 'sequence': True, 'name': 'read_file', 'args': {}},
            {'op': 'call', 'sequence': 1, 'name': 'terminal', 'args': {'command': 'forbidden'}},
            {'op': 'call', 'sequence': 1, 'name': 'delegate_task', 'args': {}},
            {'op': 'call', 'sequence': 1, 'name': 'read_file', 'args': {}, 'path': 'forbidden'},
            {'op': 'worker.adopt'},
        ]
        with tempfile.TemporaryDirectory() as directory:
            launch = SentinelLaunch(Path(directory).resolve())
            async def exercise():
                for frame in frames:
                    authority = SimpleNamespace(pending_stop_causes={})
                    worker, process = await _spawn_registered_worker(authority,
                        SimpleNamespace(session_id='sentinel-invalid'), {'generation': 1}, sentinel=launch)
                    try:
                        ToolProxy(worker, launch).bind()
                        worker.send(frame)
                        self.assertEqual(process.wait(timeout=2), 1)
                    finally:
                        worker.close()
            asyncio.run(exercise())

    @pytest.mark.platforms("posix")
    def test_owner_scope_reaps_and_deregisters_on_validation_error(self):
        from gateway.session_protected_tools import SentinelLaunch, sentinel_scope
        with tempfile.TemporaryDirectory() as directory:
            authority = SimpleNamespace(pending_stop_causes={})
            async def exercise():
                with self.assertRaises(ValueError):
                    async with sentinel_scope(authority, SimpleNamespace(session_id='scope-error'),
                            {'generation': 1}, SentinelLaunch(Path(directory).resolve())) as proxy:
                        process = proxy.worker.process
                        proxy.bind()
                        proxy.invoke('delegate_task', {})
                self.assertEqual(authority._managed_workers, {})
                self.assertEqual(authority._managed_spawning, {})
                self.assertIsNotNone(process.returncode)
            asyncio.run(exercise())

    @pytest.mark.platforms("posix")
    def test_cancel_during_actual_spawn_does_not_settle_an_admission(self):
        from unittest.mock import patch
        import subprocess
        import threading
        from gateway import session_managed_worker as managed
        from gateway.session_protected_tools import SentinelLaunch
        original = subprocess.Popen
        entered, release = (threading.Event() for _ in range(2))
        children = []
        def spawn(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            entered.set()
            assert release.wait(5)
            return child
        with tempfile.TemporaryDirectory() as directory:
            authority = SimpleNamespace(pending_stop_causes={})
            async def exercise():
                task = asyncio.create_task(managed._spawn_registered_worker(authority,
                    SimpleNamespace(session_id='cancel-spawn'), {'generation': 1},
                    sentinel=SentinelLaunch(Path(directory).resolve())))
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                task.cancel()
                try:
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                finally:
                    release.set()
                await asyncio.to_thread(children[0].wait, 5)
                self.assertEqual(authority._managed_spawning, {})
                self.assertEqual(getattr(authority, '_managed_workers', {}), {})
                self.assertFalse(hasattr(authority, 'pending_results'))
            try:
                with patch.object(managed.subprocess, 'Popen', spawn):
                    asyncio.run(exercise())
            finally:
                release.set()
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)
                    for stream in (child.stdin, child.stdout):
                        stream.close()

    def test_strict_channel_rejects_duplicate_unknown_and_oversize(self):
        from agent.protected_tool_worker import read_frame
        for raw in (b'{"op":"cancel","op":"call"}\n', b'{"op":"rpc"}\n', b'x' * 8193):
            with self.assertRaises(ValueError):
                read_frame(io.BytesIO(raw))


if __name__ == '__main__':
    unittest.main()
