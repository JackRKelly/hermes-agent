"""Native protected-tool admission and controlled, non-provider sentinel consumer.

SentinelLaunch is a source-test input, NOT an admission or a supported config key.
It runs fixed tools as the current non-root UID; it establishes no UID isolation.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import selectors
import sys
import time

from agent.protected_tool_worker import SOURCE_IMPORTS, encode_frame, read_frame
from gateway.session_managed_worker import ManagedWorker


class SentinelWorker(ManagedWorker):
    def __init__(self, process, authority, session_id):
        super().__init__(process)
        self.authority = authority
        self.session_id = session_id

    def close(self):
        try:
            super().close()
        finally:
            workers = getattr(self.authority, '_managed_workers', {})
            if workers.get(self.session_id) is self:
                workers.pop(self.session_id)

    def control(self, frame):
        if frame != {'type': 'stop'}:
            raise ValueError('unsupported sentinel control')
        if self.stopped_at is None:
            self.stopped_at = time.monotonic()
        self.stop.set()
        super().control({'op': 'cancel'})


def production_plan(request):
    """No config or client request can enable this unfinished authority boundary."""
    raise RuntimeError('protected tool admission disabled: protected launcher and inference adapter required')


@dataclass(frozen=True)
class SentinelLaunch:
    workspace: Path

    def spawn_inputs(self):
        if sys.platform not in ("darwin", "linux") or 0 in (os.getuid(), os.geteuid()):  # windows-footgun: ok — short-circuit POSIX guard
            raise ValueError('non-root sentinel execution only')
        root = self.workspace
        if type(root) is not type(Path()) or not root.is_absolute() or root.is_symlink() or root.resolve() != root:
            raise ValueError('canonical disposable sentinel workspace required')
        source = Path(__file__).resolve().parents[1]
        return [sys.executable, '-I', '-S', '-B', str(source / 'agent' / 'managed_worker.py'),
                '--sentinel-tools-v1'], {'LANG': 'C', 'LC_ALL': 'C'}, str(root)


@asynccontextmanager
async def sentinel_scope(authority, ref, row, launch):
    from gateway.session_managed_worker import _spawn_registered_worker
    worker, _ = await _spawn_registered_worker(authority, ref, row, sentinel=launch)
    try:
        yield ToolProxy(worker, launch)
    finally:
        await asyncio.to_thread(worker.close)


class ToolProxy:
    """One fixed sentinel channel, no registry/hook/host fallback, bounded lifetime."""
    def __init__(self, worker, launch):
        self.worker = worker
        self.launch = launch
        self.bound = False
        self.sequence = 0
        self.deadline = time.monotonic() + 5

    def _receive(self):
        try:
            return self._receive_frame()
        except BaseException:
            self.worker.close()
            raise

    def _receive_frame(self):
        # Unbuffered child stdout; readiness cannot hide a partial frame in a Python buffer.
        stream = self.worker.process.stdout
        body = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(stream, selectors.EVENT_READ)
            while not body.endswith(b'\n'):
                remaining = self.deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    self.worker.close()
                    raise TimeoutError('sentinel tool deadline')
                data = os.read(stream.fileno(), 1)
                if not data:
                    raise EOFError('sentinel tool closed')
                body.extend(data)
                if len(body) > 8192:
                    self.worker.close()
                    raise ValueError('sentinel tool frame exceeds bound')
        import io
        return read_frame(io.BytesIO(body))

    def _send(self, frame):
        encode_frame(frame)  # enforce the smaller tool-channel bound before native send
        self.worker.send(frame)  # same lock as the native Stop writer

    def bind(self):
        if self.bound:
            raise ValueError('already bound')
        hello = self._receive()
        if (set(hello) != {'op', 'pid', 'fds', 'environment', 'imports'} or hello['op'] != 'hello'
                or type(hello['pid']) is not int or hello['pid'] != self.worker.process.pid
                or hello['fds'] != ['0', '1', '2']
                or hello['imports'] != list(SOURCE_IMPORTS[:3])
                or type(hello['environment']) is not list
                or any(type(key) is not str for key in hello['environment'])
                or set(hello['environment']) - {'LANG', 'LC_ALL', '__CF_USER_TEXT_ENCODING'}):
            self.worker.close()
            raise ValueError('unexpected sentinel child identity or descriptors')
        self._send({'op': 'bind', 'pid': self.worker.process.pid, 'adapter': 'sentinel-only-v1'})
        self.bound = True
        return hello

    def invoke(self, name, args):
        if (not self.bound or self.worker.stop.is_set() or self.sequence >= 16
                or time.monotonic() >= self.deadline):
            raise ValueError('unbound, exhausted or expired tool channel')
        if type(name) is not str or name not in ('read_file', 'terminal') or type(args) is not dict or args:
            raise ValueError('unsupported sentinel tool or arguments')
        self.sequence += 1
        self._send({'op': 'call', 'sequence': self.sequence, 'name': name, 'args': {}})
        frame = self._receive()
        if (set(frame) != {'op', 'sequence', 'result', 'imports'} or frame['op'] != 'result'
                or type(frame['sequence']) is not int or frame['sequence'] != self.sequence
                or type(frame['result']) is not str or len(frame['result']) > 4096
                or frame['imports'] != list(SOURCE_IMPORTS)):
            self.worker.close()
            raise ValueError('invalid sentinel tool result')
        self.last_imports = frame['imports']
        return frame['result']

    def cancel(self):
        if not self.bound:
            raise ValueError('unbound tool channel')
        self.worker.interrupt()
        try:
            if self._receive() != {'op': 'cancelled'}:
                raise ValueError('invalid sentinel cancel acknowledgement')
        finally:
            self.bound = False
            self.worker.close()
