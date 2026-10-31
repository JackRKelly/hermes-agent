"""Trusted SOURCE-only scoped interfaces; no general owner RPC goes to the tool child.

These owner-private endpoints authenticate channel-specific tokens and exact
native bindings. The fresh conversation transports them through protected_ipc;
legacy tests call them directly. They are not separate protected OS identities.
Production admission remains disabled until the protected launcher is reviewed.
"""
from contextlib import closing
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from types import SimpleNamespace

from agent.protected_conversation import CapabilityClient, bounded_copy
from hermes_state_runtime import RuntimeStoreError


def _retirement_registry(authority):
    # Owner-private references survive failed acquisition and failed scope teardown.
    # setdefault installs one registry/lock even when two acquisitions race.
    registry = authority.__dict__.setdefault('_protected_retirement_state', (threading.Lock(), {}))
    authority.__dict__.setdefault('_protected_retirements', registry[1])
    return registry


class _Endpoint:
    def __init__(self, binding, channel):
        self.binding, self.channel = bounded_copy(binding), channel
        self._token = secrets.token_urlsafe(32)
        self._sequence = 0
        self._deadline = time.monotonic() + 5
        self._lock = threading.Lock()
        self._closed = False
        self._closing = False

    def client(self):
        return CapabilityClient(self.request, self._token, self.binding, self.channel)

    def request(self, frame):
        frame = bounded_copy(frame)
        with self._lock:
            if (self._closed or self._closing or type(frame) is not dict or set(frame) != {'channel', 'token', 'binding', 'sequence', 'operation', 'payload'}
                    or type(frame['token']) is not str or not hmac.compare_digest(frame['token'], self._token)
                    or frame['channel'] != self.channel or json.dumps(frame['binding'], sort_keys=True) != json.dumps(self.binding, sort_keys=True)
                    or type(frame['sequence']) is not int or frame['sequence'] != self._sequence + 1
                    or self._sequence >= 64 or time.monotonic() >= self._deadline):
                raise RuntimeStoreError('protected_capability_denied')
            self._sequence += 1
            return self._invoke(frame['operation'], frame['payload'])

    def _invoke(self, operation, payload):
        raise NotImplementedError


class ScopedInference(_Endpoint):
    """One preselected reviewed provider callable, no name/URL/key/config selection.

    Tests supply an explicitly controlled NO-REAL-PROVIDER callable returning the
    existing NormalizedResponse type. No production SDK/auth adapter is claimed.
    """
    def __init__(self, complete, binding):
        super().__init__(binding, 'inference')
        self._complete = complete

    def _invoke(self, operation, payload):
        if (operation != 'complete' or type(payload) is not dict or set(payload) != {'messages', 'tools'}
                or type(payload['messages']) is not list or type(payload['tools']) is not list):
            raise RuntimeStoreError('protected_capability_denied')
        return self._complete(payload['messages'], payload['tools'])


class NativeLifecycle(_Endpoint):
    """Owner-private reservation/claim scope; only check and bounded transcript append.

    No caller-selected DB, session, card, operation, PID or generation. Canonical
    secrets/DB objects never enter ToolProxy's fixed stdin/stdout protocol. This
    layer does not settle admissions, complete cards, renew leases or schedule work.
    """
    @classmethod
    def reserve(cls, authority, *, admission_id, principal_id, process, context):
        from gateway.session_worker_reservation import reserve_admission_worker
        from hermes_cli.process_identity import worker_birth
        from hermes_state_runtime import adopt_worker_execution
        from gateway.session_worker import _claim
        import os
        import psutil
        self = cls.__new__(cls)
        self._authority, self._process = authority, process
        container = getattr(process, '_protected_container', None)
        self._container_binding = container.attest() if container is not None else None
        # Trusted owner policy input only; never accepted from an inference/tool frame.
        self._context = bounded_copy(context)
        self._card_check()
        from hermes_state_runtime import get_session_admission
        admitted = get_session_admission(authority.db, admission_id=admission_id)
        expected = {k: self._context[k] for k in ('board', 'task_id', 'run_id')}
        if admitted is None or admitted['payload'].get('kanban_card_v1') != expected:
            raise RuntimeStoreError('protected_card_binding_mismatch')
        hello = {'type': 'hello', 'pid': process.pid, 'birth': worker_birth(psutil.Process(process.pid)), 'ancestors': [os.getpid()]}
        self._admission_id = admission_id
        self._connection = SimpleNamespace(authority=authority,
            actor=SimpleNamespace(subject=principal_id, profile_id=authority.profile_id, capabilities={'worker:adopt'}))
        self._native_sequence = 0
        self._lock, self._closed = threading.Lock(), False
        self._scope = reserve_admission_worker(authority, admission_id=admission_id, process=process,
            principal_id=principal_id, hello=hello)
        try:
            super(NativeLifecycle, self).__init__({k: self._scope[k] for k in
                ('profile_id', 'session_id', 'execution_id', 'generation', 'epoch')} |
                {k: self._context[k] for k in ('board', 'task_id', 'run_id')} |
                ({'docker_tool_v1': self._container_binding} if self._container_binding is not None else {}), 'lifecycle')
            identity = {k: v for k, v in self._scope.items() if k != 'epoch'}
            claim = _claim(self._connection, None, identity)
            adopt_worker_execution(authority.db, epoch=self._scope['epoch'], execution_id=self._scope['execution_id'],
                session_id=self._scope['session_id'], generation=self._scope['generation'], adoption_secret=claim)
            self._check()
            return self
        except BaseException:
            self.close()
            raise

    def _card_check(self):
        context = self._context
        if (type(context.get('run_id')) is not int or type(context.get('claim_lock')) is not str
                or not context['claim_lock']):
            raise RuntimeStoreError('invalid_kanban_claim')
        path = Path(context['db']).resolve(strict=True)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.2)) as conn:
            conn.row_factory = sqlite3.Row
            task = conn.execute('SELECT status,current_run_id,claim_lock,claim_expires FROM tasks WHERE id=?',
                (context['task_id'],)).fetchone()
            run = conn.execute('SELECT status,task_id,claim_lock FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
            if (task is None or run is None or task['status'] != 'running' or run['status'] != 'running'
                    or task['current_run_id'] != context['run_id'] or run['task_id'] != context['task_id']
                    or task['claim_lock'] != context['claim_lock'] or run['claim_lock'] != context['claim_lock']
                    or type(task['claim_expires']) is not int or task['claim_expires'] <= time.time()):
                raise RuntimeStoreError('stale_kanban_claim')

    def _check(self):
        from gateway.session_worker_reservation import verify_worker_identity
        from gateway.session_worker import _claim, _verify
        from hermes_state_runtime import _epoch, _admission, _worker_assignment
        scope = self._scope
        container = getattr(self._process, '_protected_container', None)
        if container is not None:
            if (container.attest() != self._container_binding
                    or self._container_binding['generation'] != str(scope['generation'])):
                raise RuntimeStoreError('protected_container_binding_mismatch')
        verify_worker_identity(self._process, {'type': 'hello', 'pid': scope['pid'], 'birth': scope['birth'], 'ancestors': []})
        identity = {k: v for k, v in scope.items() if k != 'epoch'}
        _verify(self._connection, None, identity, _claim(self._connection, None, identity))
        with self._authority.db._read_ctx() as conn:
            _epoch(conn, scope['epoch'])
            row = _admission(conn, self._admission_id)
            worker = _worker_assignment(conn, scope['execution_id'], scope['session_id'], scope['generation'])
            if (row['status'] != 'started' or row['owner_epoch'] != scope['epoch']
                    or row['generation'] != scope['generation'] or worker['status'] != 'running'
                    or worker['owner_epoch'] != scope['epoch']
                    or row['principal_id'] != self._connection.actor.subject
                    or json.loads(row['payload_json']).get('kanban_card_v1') !=
                        {k: self._context[k] for k in ('board', 'task_id', 'run_id')}):
                raise RuntimeStoreError('stale_generation')
        self._card_check()
        guard = getattr(self, '_policy_guard', None)
        if guard is not None:
            guard()

    def close(self):
        """Retire through the native loss contract; never fabricate completed admission.

        The managed worker scope, not this interface, owns process close/reap.
        Revoked generations/epochs are not repaired or overwritten by teardown.
        """
        from gateway.session_worker_reservation import lose_admission_worker
        with self._lock:
            if self._closed:
                return
            self._closing = True
            lock, pending = _retirement_registry(self._authority)
            key = self._scope['execution_id']
            with lock:
                pending[key] = self
            try:
                lose_admission_worker(self._authority, {'admission_id': self._admission_id}, self._scope)
            except RuntimeStoreError as exc:
                if str(exc) not in ('stale_generation', 'stale_epoch'):
                    raise
            self._closed = True
            with lock:
                if pending.get(key) is self:
                    pending.pop(key)

    def _invoke(self, operation, payload):
        self._check()
        if operation == 'check' and payload == {}:
            return None
        evidence = getattr(self, '_observations', None)
        if operation == 'append' and evidence is not None:
            rows = evidence.authorize_append(payload)
        elif operation == 'append' and type(payload) is dict and set(payload) == {'messages'} and evidence is None:
            rows = payload['messages']  # legacy synchronous routing fixtures only
        else:
            raise RuntimeStoreError('protected_capability_denied')
        if (type(rows) is not list or not 1 <= len(rows) <= 8 or any(type(row) is not dict or
                set(row) - {'role', 'content', 'tool_calls', 'tool_call_id', 'tool_name'} or
                row.get('role') not in ('user', 'assistant', 'tool') for row in rows)):
            raise RuntimeStoreError('protected_capability_denied')
        from hermes_state_runtime import mutate_worker_execution
        self._native_sequence += 1
        scope = self._scope
        result = mutate_worker_execution(self._authority.db, epoch=scope['epoch'],
            execution_id=scope['execution_id'], session_id=scope['session_id'], generation=scope['generation'],
            sequence=self._native_sequence, operation='transcript.append', payload={'messages': rows})
        if evidence is not None:
            evidence.commit_append(payload)
        self._check()
        return result
