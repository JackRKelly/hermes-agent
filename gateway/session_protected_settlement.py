"""Owner-only successful SOURCE settlement; never a child RPC or live admission.

The terminal outcome + full correlation receipt commit atomically in the owner
store, under the card writer lock. Card completion uses the canonical API and is
an idempotent projection of that receipt, NOT a cross-WAL atomic transaction.
A crash/board commit failure can leave a completed owner receipt with an
unprojected card: retry this exact owner before teardown; never rerun inference.
"""
from contextlib import closing
import base64
import hashlib
import json
from pathlib import Path
import time

from hermes_state_runtime import RuntimeStoreError


PREFIX = 'protected.success.v1.'


def _deny():
    raise RuntimeStoreError('protected_success_denied')


def settle_success(runner, names):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from hermes_state_runtime import _admission, _epoch, _session, settle_session_input
    owner = runner.turn._native_owner
    scope, context, db = owner._scope, owner._context, owner._authority.db
    key = PREFIX + owner._admission_id
    phase_bridge = getattr(runner, '_phase_bridge', None)
    container = getattr(runner.turn.proxy.worker, 'container', None)
    if type(names) is not tuple:
        _deny()
    with db._read_ctx() as conn:
        old = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
    if old:
        receipt = json.loads(old[0])
        if tuple(receipt['artifacts']) != tuple(sorted(names)):
            _deny()
    else:
        if runner._closed.is_set() or runner.turn.proxy.worker.stop.is_set():
            _deny()
        # Owner-observed normalized response plus exact committed transcript,
        # not a child reply, returned event string, close() or EOF.
        owner._check()
        result = owner._observations.result()
        runner.turn.proxy.bound = False  # irreversible tool revocation, distinct from cancellation
        container = getattr(runner.turn.proxy.worker, 'container', None)
        if container is not None:
            if not names:
                _deny()
            artifacts = container.export(names)
        elif names:
            _deny()
        else:
            artifacts = {}
        owner._check()
        receipt = dict(outcome='completed', admission_id=owner._admission_id,
            session_id=scope['session_id'], execution_id=scope['execution_id'],
            generation=scope['generation'], epoch=scope['epoch'],
            card={k: context[k] for k in ('board', 'task_id', 'run_id')},
            transcript_sha256=hashlib.sha256(json.dumps(owner._observations.messages, sort_keys=True).encode()).hexdigest(),
            result=result, artifacts={name: dict(sha256=hashlib.sha256(body).hexdigest(),
                body=base64.b64encode(body).decode()) for name, body in sorted(artifacts.items())})
    if phase_bridge is not None:
        from dataclasses import asdict
        receipt.update(phase_policy=asdict(phase_bridge.phase_policy), binding=phase_bridge.binding,
            namespace=dict(owner._container_binding, name=container.name, daemon=container.runtime.daemon, endpoint=container.runtime.endpoint))

    expected = dict(admission_id=owner._admission_id, session_id=scope['session_id'],
        execution_id=scope['execution_id'], generation=scope['generation'], epoch=scope['epoch'],
        card={k: context[k] for k in ('board', 'task_id', 'run_id')})
    if any(receipt[k] != value for k, value in expected.items()):
        _deny()

    def native_fence(conn, *, terminal):
        _epoch(conn, scope['epoch'])
        row = _admission(conn, owner._admission_id)
        worker = conn.execute('SELECT * FROM worker_executions WHERE execution_id=?', (scope['execution_id'],)).fetchone()
        if ((not terminal and (runner._closed.is_set() or runner.turn.proxy.worker.stop.is_set()))
                or row['status'] != ('terminal' if terminal else 'started')
                or (terminal and row['outcome'] != 'completed')
                or row['generation'] != scope['generation'] or row['owner_epoch'] != scope['epoch']
                or row['target_session_id'] != scope['session_id']
                or row['principal_id'] != owner._connection.actor.subject
                or json.loads(row['payload_json']).get('kanban_card_v1') != receipt['card']
                or _session(conn, scope['session_id'])['runtime_generation'] != scope['generation']
                or worker is None or worker['session_id'] != scope['session_id']
                or worker['generation'] != scope['generation'] or worker['owner_epoch'] != scope['epoch']):
            _deny()
        observed = owner._observations.messages
        rows = conn.execute('SELECT role,content,tool_calls,tool_call_id,tool_name FROM messages WHERE session_id=? ORDER BY id',
            (scope['session_id'],)).fetchall()
        messages = []
        for row in rows:
            value = dict(row)
            if value['tool_calls'] is not None:
                value['tool_calls'] = json.loads(value['tool_calls'])
            if len(messages) >= len(observed):
                _deny()
            messages.append({k: value[k] for k in observed[len(messages)]})
        if messages != observed or hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest() != receipt['transcript_sha256']:
            _deny()

    def guard(conn):
        # This runs inside complete_task's BEGIN IMMEDIATE. No stale lease can
        # turn a valid old provider result into a newly completed card.
        task = conn.execute('SELECT * FROM tasks WHERE id=?', (context['task_id'],)).fetchone()
        run = conn.execute('SELECT * FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
        if (runner._closed.is_set() or runner.turn.proxy.worker.stop.is_set()
                or task is None or run is None or task['status'] != ('blocked' if phase_bridge is not None else 'running') or run['status'] != 'running'
                or task['current_run_id'] != context['run_id'] or run['task_id'] != context['task_id']
                or task['claim_lock'] != context['claim_lock'] or run['claim_lock'] != context['claim_lock']
                or not task['claim_expires'] or task['claim_expires'] <= time.time()):
            _deny()
        if old:
            with db._read_ctx() as native:
                native_fence(native, terminal=True)
        else:
            def terminal_write(native, row):
                # _retire_admission_workers already ran in this same transaction.
                native_fence(native, terminal=False)
                native.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (key, json.dumps(receipt, sort_keys=True)))
            settle_session_input(db, epoch=scope['epoch'], admission_id=owner._admission_id,
                generation=scope['generation'], outcome='completed',
                result={'result': receipt['result'], 'usage': {}}, _terminal_write=terminal_write)

    if phase_bridge is not None:
        from gateway.session_protected_phase import settle_held
        return settle_held(runner, receipt, guard, native_fence)

    with closing(connect(Path(context['db']))) as conn:
        task = kb.get_task(conn, context['task_id'])
        if not (old and task is not None and task.status == 'done'):
            # Model prose must not select host artifacts or notification text.
            summary = 'Protected success ' + hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
            if not kb.complete_task(conn, context['task_id'], result=summary, summary=summary,
                    metadata={'protected_success_v1': receipt}, expected_run_id=context['run_id'],
                    fire_lifecycle_hook=False, _completion_guard=guard):
                _deny()
        with kb.write_txn(conn):
            task = kb.get_task(conn, context['task_id'])
            run = conn.execute('SELECT status,outcome,metadata FROM task_runs WHERE id=? AND task_id=?',
                (context['run_id'], context['task_id'])).fetchone()
            if (task is None or task.status != 'done' or task.current_run_id is not None or run is None
                    or run['status'] != 'done' or run['outcome'] != 'completed'
                    or json.loads(run['metadata'] or '{}').get('protected_success_v1') != receipt):
                _deny()
            with db._read_ctx() as native:
                saved = native.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
                if saved is None or json.loads(saved[0]) != receipt:
                    _deny()
                native_fence(native, terminal=True)
    return receipt
