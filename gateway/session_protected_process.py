"""SOURCE owner integration: fresh frozen conversation, authenticated Unix IPC.

Production admission is still unconditionally disabled. The owner supplies ONE
already-selected async provider SDK method + Hermes ProviderTransport + close
method; no discovery, fallback, key lookup or URL/model selection comes from IPC.
Tests use NO-REAL-PROVIDER async fixtures. This is not cross-UID containment.
"""
import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
import hmac
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

from agent.protected_ipc import current_uid, encode, receive, verify_peer
from agent.transports.types import NormalizedResponse
from gateway.session_protected_capabilities import _Endpoint
from gateway.session_protected_provider import SelectedAsyncProvider
from gateway.session_protected_observations import OwnerObservations
from gateway.session_protected_scope import _drain_owned, protected_turn_scope
from hermes_state_runtime import RuntimeStoreError


class _Tools(_Endpoint):
    def __init__(self, turn):
        super().__init__(turn.lifecycle.binding, 'tools')
        self.turn = turn

    def _invoke(self, operation, payload):
        if (operation != 'call' or type(payload) is not dict or set(payload) != {'name', 'args'}
                or payload['name'] not in ('read_file', 'terminal') or type(payload['args']) is not dict):
            raise RuntimeStoreError('protected_capability_denied')
        if self.turn.proxy.worker.stop.is_set():
            raise RuntimeStoreError('protected_capability_denied')
        self.turn._native_owner._check()
        evidence = self.turn._native_owner._observations
        call_id = evidence.before_tool(payload['name'], payload['args'])
        result = self.turn.proxy.invoke(payload['name'], payload['args'])
        self.turn._native_owner._check()
        if self.turn.proxy.worker.stop.is_set():
            raise RuntimeStoreError('protected_capability_denied')
        evidence.observe_tool(call_id, result)
        return result


class ProtectedProcess:
    def __init__(self, turn, provider):
        self.turn, self.provider = turn, provider
        self.process = None
        self.threads, self.connections = [], []
        self._closed = threading.Event()
        self._sockets_lock = threading.Lock()
        self._read_task = None
        self._close_task = None
        self._start_task = None
        self._used = False
        self._cleaned = set()
        turn._native_owner._observations = OwnerObservations(list(turn.tools))
        self.imports = []
        self._directory = None
        self.listener = None
        self.endpoints = {'inference': turn._inference_endpoint,
                          'lifecycle': turn._native_owner, 'tools': _Tools(turn)}

    def _receive(self, conn):
        # BSD timed socket reads need not wake on another thread's close/shutdown.
        # Poll revocation without resetting the existing five-second frame bound,
        # including partial frames; receive() retains its accumulated bytes.
        deadline = time.monotonic() + 5
        conn.settimeout(.1)
        def read(size):
            while not self._closed.is_set():
                if time.monotonic() >= deadline:
                    raise TimeoutError('protected channel frame deadline')
                try:
                    return conn.recv(size)
                except TimeoutError:
                    continue
            raise EOFError('protected channel revoked')
        return receive(read)

    def _serve(self):
        conn = None
        channel = 'unclaimed'
        try:
            for _ in range(50):
                if self._closed.is_set():
                    return
                try:
                    conn, _ = self.listener.accept()
                    break
                except TimeoutError:
                    continue
            else:
                raise TimeoutError('protected channel handshake deadline')
            with self._sockets_lock:
                self.connections.append(conn)
            if self._closed.is_set():
                return
            conn.settimeout(5)
            verify_peer(conn, pid=self.process.pid, uid=current_uid())
            hello = self._receive(conn)
            if type(hello) is not dict or set(hello) != {'channel', 'token'} or type(hello['channel']) is not str:
                raise RuntimeStoreError('protected_capability_denied')
            endpoint = self.endpoints.get(hello['channel'])
            if (endpoint is None or type(hello['token']) is not str
                    or not hmac.compare_digest(hello['token'], endpoint._token)):
                raise RuntimeStoreError('protected_capability_denied')
            # Never permit two sockets to share a channel/replay counter.
            with self._sockets_lock:
                if hello['channel'] in self._claimed:
                    raise RuntimeStoreError('protected_capability_denied')
                self._claimed.add(hello['channel'])
                channel = endpoint.channel
            for _ in range(64):
                if self._closed.is_set():
                    return
                frame = self._receive(conn)
                value = endpoint.request(frame)
                if type(value) is NormalizedResponse:
                    value = asdict(value)
                if self._closed.is_set():
                    return
                conn.settimeout(5)
                conn.sendall(encode({'ok': True, 'value': value}))
        except (Exception, asyncio.CancelledError) as error:  # health: allow BLE001 -- sanitize peer/auth, store, SDK and programming faults at the wire boundary
            # Type-only owner diagnostic; no exception message, traceback, token,
            # unauthenticated channel label, request content or credential.
            logging.getLogger(__name__).warning('protected_endpoint_denied channel=%s exception=%s reason=boundary_fault',
                channel, type(error).__name__)
            if conn is not None:
                with suppress(OSError):
                    conn.sendall(encode({'ok': False, 'value': None}))
        finally:
            if conn is not None:
                conn.close()

    async def _start(self, prompt):
        if self._closed.is_set():
            raise RuntimeError('protected conversation closed')
        if self._start_task is None:
            self._start_task = asyncio.create_task(self._acquire(prompt))
        await asyncio.shield(self._start_task)
        if self._closed.is_set():
            raise RuntimeError('protected conversation closed')

    async def _acquire(self, prompt):
        self._directory = tempfile.TemporaryDirectory(prefix='pc-')
        path = str(Path(self._directory.name) / 's')
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # close() on another thread need not wake a BSD accept. Short bounded
        # accept polls allow revocation to join even an unclaimed channel.
        self.listener.settimeout(.1)
        self.listener.bind(path)
        self.listener.listen(3)
        source = (await asyncio.to_thread(Path(__file__).resolve)).parents[1]
        if self._closed.is_set():
            return
        from gateway.session_worker_spawn import acquire_process
        self.process = await acquire_process(subprocess.Popen,
            [sys.executable, '-I', '-S', '-B', str(source / 'hermes_protected_entry.py')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={'LANG': 'C', 'LC_ALL': 'C'}, cwd=self.turn.proxy.launch.workspace,
            close_fds=True, start_new_session=True, bufsize=0)
        if self._closed.is_set():
            return
        self._claimed = set()
        for name in self.endpoints:
            from agent.memory_provider import spawn_context_thread
            thread = spawn_context_thread(self._serve, name='protected-' + name, daemon=True)
            self.threads.append(thread)
            thread.start()
        config = {'path': path, 'owner_pid': os.getpid(), 'owner_uid': current_uid(), 'prompt': prompt,
                  'channels': {name: {'channel': name, 'token': endpoint._token, 'binding': endpoint.binding}
                               for name, endpoint in self.endpoints.items()}}
        self.process.stdin.write(encode(config))
        self.process.stdin.close()

    async def run_conversation(self, prompt):
        if self._closed.is_set():
            raise RuntimeError('protected conversation closed')
        if self._used:
            raise RuntimeError('protected conversation is single use')
        self._used = True
        if type(prompt) is not str or not prompt or len(prompt.encode()) > 4096:
            raise ValueError('invalid protected prompt')
        self.turn._native_owner._observations.start(prompt)
        stopper = asyncio.create_task(self.turn.proxy.worker.stop.wait())
        try:
            await self._start(prompt)
            self._read_task = asyncio.create_task(asyncio.to_thread(receive, self.process.stdout.read))
            done, _ = await asyncio.wait({self._read_task, stopper}, timeout=5,
                return_when=asyncio.FIRST_COMPLETED)
            if self.turn.proxy.worker.stop.is_set():
                raise RuntimeError('protected conversation stopped')
            if self._read_task not in done:
                raise TimeoutError('protected conversation deadline')
            reply = self._read_task.result()
            if type(reply) is not dict or set(reply) != {'result', 'imports'}:
                raise RuntimeError('protected conversation failed')
            self.imports = reply['imports']
            result = self.turn._native_owner._observations.result()
            if reply['result'] != result:
                raise RuntimeStoreError('protected_observation_denied')
            return result
        except BaseException:
            cancellations = []
            await _drain_owned(asyncio.create_task(self.close()), cancellations)
            if cancellations:
                raise asyncio.CancelledError
            raise
        finally:
            stopper.cancel()
            with suppress(asyncio.CancelledError):
                await stopper

    def _reap_process(self):
        from gateway.session_managed_worker import ManagedWorker
        control = ManagedWorker(self.process)
        if self.process.poll() is None:
            control._signal_group(kill=False)
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            control._signal_group(kill=True)
            self.process.wait(timeout=2)
        if self.process.poll() is None:
            raise RuntimeError('protected child not reaped')

    def _reap(self):
        errors = []
        def attempt(key, action):
            if key in self._cleaned:
                return
            try:
                action()
                self._cleaned.add(key)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                errors.append(error)
        if self.listener is not None:
            attempt('listener', self.listener.close)
        with self._sockets_lock:
            connections = tuple(self.connections)
        for conn in connections:
            with suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            attempt(('socket', id(conn)), conn.close)
        if self.process is not None:
            attempt('process', self._reap_process)
        for thread in self.threads:
            if thread.ident is not None:
                attempt(('thread', id(thread)), lambda: self._join_thread(thread))
            if thread.is_alive():
                errors.append(RuntimeError('protected IPC thread did not exit'))
        if self.process is not None:
            attempt('stdin', self.process.stdin.close)
            attempt('stdout', self.process.stdout.close)
        if self._directory is not None:
            attempt('directory', self._directory.cleanup)
        if errors:
            raise errors[0]

    @staticmethod
    def _join_thread(thread):
        thread.join(timeout=6)
        if thread.is_alive():
            raise RuntimeError('protected IPC thread did not exit')

    async def _finish_close(self, retry_provider):
        errors = []
        self.turn.proxy.worker.stop.set()
        for endpoint in self.endpoints.values():
            endpoint._closing = True
        self.provider.begin_close()
        # An acquisition already in progress owns its late Popen handle until
        # assigned here. Never finish close while it can still produce a child.
        if self._start_task is not None:
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(self._start_task)
        try:
            await asyncio.to_thread(self._reap)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            errors.append(error)
        if self._read_task is not None:
            done, _ = await asyncio.wait({self._read_task}, timeout=.5)
            if self._read_task not in done:
                errors.append(RuntimeError('protected pipe read still pending'))
            else:
                with suppress(Exception, asyncio.CancelledError):
                    self._read_task.result()
        # The SDK cannot prevent attempts to reap our resources or the enclosing
        # native reservation. Its own uncooperative residue remains retained.
        try:
            await self.provider.close(retry=retry_provider)
        except (OSError, RuntimeError) as error:
            errors.append(error)
        if errors:
            raise errors[0]
        _process_closes(self.turn._native_owner._authority).pop(id(self), None)

    async def close(self, *, retry_provider=False):
        if self._close_task is not None and self._close_task.done():
            if not self._close_task.cancelled() and self._close_task.exception() is None:
                return
            self._close_task = None
        if self._close_task is None:
            self._closed.set()
            _process_closes(self.turn._native_owner._authority)[id(self)] = self
            self._close_task = asyncio.create_task(self._finish_close(retry_provider))
        await asyncio.shield(self._close_task)


def _process_closes(authority):
    return authority.__dict__.setdefault('_protected_process_closes', {})


async def retry_protected_process_closes(authority):
    """Owner-only retry of exact retained process/provider handles, no new IDs."""
    errors, cancellations = [], []
    for runner in tuple(_process_closes(authority).values()):
        try:
            await _drain_owned(asyncio.create_task(runner.close(retry_provider=True)), cancellations)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            errors.append(error)
    if errors:
        raise errors[0]
    if cancellations:
        raise asyncio.CancelledError


def _saved_policy(authority, ref, row, launch, principal_id, context, provider):
    from hermes_state_local import local_receipt
    from hermes_state_runtime import get_session_admission
    saved = local_receipt(authority.db, ref.session_id)
    current = get_session_admission(authority.db, admission_id=row['admission_id'])
    if (saved is None or saved['profile_id'] != authority.profile_id or saved['principal_id'] != principal_id
            or current is None or current['target_session_id'] != ref.session_id
            or current['principal_id'] != principal_id or current['status'] != 'started'
            or current['owner_epoch'] != authority.epoch or type(row.get('generation')) is not int
            or current['generation'] != row['generation']):
        raise RuntimeStoreError('protected_owner_policy_mismatch')
    from gateway.session_policy import restore_policy
    policy = saved['policy']
    restored = restore_policy(policy)
    if (restored.source != 'kanban' or restored.model != provider.model or restored.provider != provider.provider_id
            or json.dumps(json.loads(policy['kanban_json']), sort_keys=True) != json.dumps(context, sort_keys=True)
            or Path(policy['cwd']).resolve() != launch.workspace
            or Path(context['workspace']).resolve() != launch.workspace):
        raise RuntimeStoreError('protected_owner_policy_mismatch')
    return hashlib.sha256(json.dumps(policy, sort_keys=True, allow_nan=False).encode()).hexdigest()


@asynccontextmanager
async def protected_process_scope(authority, ref, row, launch, *, principal_id, context, provider):
    """One owned fresh conversation plus cancellable selected async inference.

    Unlike legacy callback-only SOURCE fixtures, cancelling this scope stops the
    actual async call, closes the provider and all IPC, retires native reservation,
    and reaps both children. No live admission or protected launcher is enabled.
    """
    if type(provider) is not SelectedAsyncProvider:
        raise ValueError('explicit selected async provider required')
    try:
        policy_digest = await asyncio.to_thread(_saved_policy, authority, ref, row, launch, principal_id, context, provider)
        loop = asyncio.get_running_loop()
        def complete(messages, tools):
            # Enforce the frozen schema on the OWNER too, not only in the child.
            # The channel cannot smuggle provider-hosted tools or a discovery schema.
            owner._check()
            if json.dumps(tools, sort_keys=True) != json.dumps(list(turn.tools), sort_keys=True):
                raise RuntimeStoreError('protected_capability_denied')
            owner._observations.before_inference(messages, tools)
            future = provider.submit(messages, tools, loop)
            try:
                result = future.result(timeout=5)
                owner._check()
                owner._observations.observe_response(result)
                return result
            finally:
                if not future.done():
                    future.cancel()
        async with protected_turn_scope(authority, ref, row, launch, principal_id=principal_id,
                context=context, complete=complete) as turn:
            runner = ProtectedProcess(turn, provider)
            try:
                owner = turn._native_owner
                def guard():
                    if (turn.proxy.worker.stop.is_set() or
                            _saved_policy(authority, ref, row, launch, principal_id, context, provider) != policy_digest):
                        raise RuntimeStoreError('protected_owner_policy_mismatch')
                owner._policy_guard = guard
                binding = dict(owner.binding, owner_observations_v1=True, policy_digest=policy_digest, owner_pid=os.getpid(),
                    worker_pid=owner._scope['pid'], worker_birth=owner._scope['birth'])
                for endpoint in runner.endpoints.values():
                    endpoint.binding = dict(binding)
                guard()
                yield runner
            finally:
                cancellations = []
                await _drain_owned(asyncio.create_task(runner.close()), cancellations)
                if cancellations:
                    raise asyncio.CancelledError
    finally:
        cancellations = []
        await _drain_owned(asyncio.create_task(provider.close()), cancellations)
        if cancellations:
            raise asyncio.CancelledError
