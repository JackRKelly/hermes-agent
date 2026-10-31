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
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading

from agent.protected_ipc import current_uid, encode, receive, verify_peer
from agent.transports.types import NormalizedResponse
from gateway.session_protected_capabilities import _Endpoint
from gateway.session_protected_scope import _drain_owned, protected_turn_scope
from hermes_state_runtime import RuntimeStoreError


class SelectedAsyncProvider:
    """Pinned owner adapter for an async SDK create method and ProviderTransport.

    Example shape: transport=ChatCompletionsTransport(...),
    create=explicit_client.chat.completions.create, aclose=explicit_client.close.
    Construction/authentication is an owner responsibility, outside this API.
    Async cancellation must be supported by the selected SDK; no to_thread call.
    """
    def __init__(self, *, model, provider_id, transport, create, aclose):
        if (type(model) is not str or not model or type(provider_id) is not str or not provider_id
                or not callable(create) or not callable(aclose)):
            raise ValueError('explicit selected async provider required')
        self.model, self.provider_id, self.transport = model, provider_id, transport
        self._create, self._aclose = create, aclose
        self._tasks = set()
        self._closed = False
        self._close_task = None

    async def complete(self, messages, tools):
        if self._closed:
            raise RuntimeError('protected provider closed')
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            kwargs = self.transport.build_kwargs(self.model, messages, tools)
            response = await self._create(**kwargs)
            if self._closed:
                raise RuntimeError('protected provider closed')
            result = self.transport.normalize_response(response)
            if type(result) is not NormalizedResponse:
                raise ValueError('provider did not normalize response')
            return result
        finally:
            self._tasks.discard(task)

    async def _finish_close(self):
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self._aclose()

    async def close(self):
        # One close operation, shielded from eager/scope concurrent cancellation.
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._finish_close())
        await asyncio.shield(self._close_task)


class _Tools(_Endpoint):
    def __init__(self, turn):
        super().__init__(turn.lifecycle.binding, 'tools')
        self.turn = turn

    def _invoke(self, operation, payload):
        if (operation != 'call' or type(payload) is not dict or set(payload) != {'name', 'args'}
                or payload['name'] not in ('read_file', 'terminal') or payload['args'] != {}):
            raise RuntimeStoreError('protected_capability_denied')
        if self.turn.proxy.worker.stop.is_set():
            raise RuntimeStoreError('protected_capability_denied')
        self.turn._native_owner._check()
        result = self.turn.proxy.invoke(payload['name'], payload['args'])
        self.turn._native_owner._check()
        if self.turn.proxy.worker.stop.is_set():
            raise RuntimeStoreError('protected_capability_denied')
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
        self._used = False
        self.imports = []
        self._directory = None
        self.listener = None
        self.endpoints = {'inference': turn._inference_endpoint,
                          'lifecycle': turn._native_owner, 'tools': _Tools(turn)}

    def _serve(self):
        conn = None
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
            hello = receive(conn.recv)
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
            for _ in range(64):
                if self._closed.is_set():
                    return
                frame = receive(conn.recv)
                value = endpoint.request(frame)
                if type(value) is NormalizedResponse:
                    value = asdict(value)
                if self._closed.is_set():
                    return
                conn.sendall(encode({'ok': True, 'value': value}))
        except (Exception, asyncio.CancelledError):  # health: allow BLE001 -- sanitize arbitrary selected SDK errors at the bounded wire boundary; never log secrets
            # Invalid peer/capability/provider failure closes this endpoint. Never
            # serialize provider exception bodies, credentials or traceback text.
            if conn is not None:
                with suppress(OSError):
                    conn.sendall(encode({'ok': False, 'value': None}))
        finally:
            if conn is not None:
                conn.close()

    async def _start(self, prompt):
        self._directory = tempfile.TemporaryDirectory(prefix='pc-')
        path = str(Path(self._directory.name) / 's')
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # close() on another thread need not wake a BSD accept. Short bounded
        # accept polls allow revocation to join even an unclaimed channel.
        self.listener.settimeout(.1)
        self.listener.bind(path)
        self.listener.listen(3)
        source = (await asyncio.to_thread(Path(__file__).resolve)).parents[1]
        from gateway.session_worker_spawn import acquire_process
        self.process = await acquire_process(subprocess.Popen,
            [sys.executable, '-I', '-S', '-B', str(source / 'hermes_protected_entry.py')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={'LANG': 'C', 'LC_ALL': 'C'}, cwd=self.turn.proxy.launch.workspace,
            close_fds=True, start_new_session=True, bufsize=0)
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
        if self._used:
            raise RuntimeError('protected conversation is single use')
        self._used = True
        if type(prompt) is not str or not prompt or len(prompt.encode()) > 4096:
            raise ValueError('invalid protected prompt')
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
            return reply['result']
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

    def _reap(self):
        if self.listener is not None:
            self.listener.close()
        with self._sockets_lock:
            for conn in self.connections:
                with suppress(OSError):
                    conn.shutdown(socket.SHUT_RDWR)
                conn.close()
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for thread in self.threads:
            if thread.ident is not None:
                thread.join(timeout=6)
            if thread.is_alive():
                raise RuntimeError('protected IPC thread did not exit')
        if self.process is not None:
            self.process.stdin.close()
            self.process.stdout.close()
        if self._directory is not None:
            self._directory.cleanup()

    async def _finish_close(self):
        self._closed.set()
        self.turn.proxy.worker.stop.set()
        # Revoke before cancellation; no late response can start a tool.
        for endpoint in self.endpoints.values():
            endpoint._closing = True
        try:
            await self.provider.close()
        finally:
            await asyncio.to_thread(self._reap)
            if self._read_task is not None:
                with suppress(Exception):
                    await asyncio.shield(self._read_task)

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._finish_close())
        await asyncio.shield(self._close_task)


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
            future = asyncio.run_coroutine_threadsafe(provider.complete(messages, tools), loop)
            try:
                result = future.result(timeout=5)
                owner._check()
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
                binding = dict(owner.binding, policy_digest=policy_digest, owner_pid=os.getpid(),
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
