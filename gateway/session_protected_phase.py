"""Typed owner-only held phases on the existing canonical board/event graph.

The model never calls these functions. The broker's existing pinned verifier
signs exact scopes; publication dictionaries are data, never authorization.
Idle phases are BLOCKED even for a pre-protection dispatcher binary.
"""
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, UTC
import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Literal, NoReturn

from hermes_state_runtime import RuntimeStoreError


@dataclass(frozen=True)
class HeldPhasePolicy:
    graph_digest: str
    campaign_key: str
    card_id: str
    board: str
    task_id: str
    role: Literal['builder', 'reviewer']
    implementer_profile: str
    reviewer_profile: str
    input_head: str

    def __post_init__(self):
        from hermes_cli import kanban_db as kb
        if (any(type(v) is not str or not v or len(v) > 256 for v in asdict(self).values())
                or not re.fullmatch('[a-f0-9]{64}', self.graph_digest)
                or not re.fullmatch('[a-f0-9]{40}', self.input_head)
                or self.role not in ('builder', 'reviewer')
                or any(type(v) is not str or not v or len(v) > 256 for v in asdict(self).values())
                or any(not re.fullmatch('[a-zA-Z0-9_-]+', v) for v in (self.implementer_profile, self.reviewer_profile))
                or self.implementer_profile == self.reviewer_profile):
            raise ValueError('protected_phase_policy_invalid')
        kb._require_slug(self.board)


def _deny() -> NoReturn:
    raise RuntimeStoreError('protected_phase_denied')


def _copy(value):
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _owner(authority):
    from gateway.session_authority import SessionAuthority
    from hermes_state_runtime import _epoch
    if type(authority) is not SessionAuthority or authority.retired:
        _deny()
    authority._require_admission_open()
    with authority.db._read_ctx() as conn:
        _epoch(conn, authority.epoch)


def _path(authority, policy):
    home = Path(authority.runner.kanban_home).resolve(strict=True)
    path = home / ('kanban.db' if policy.board == 'default' else f'kanban/boards/{policy.board}/kanban.db')
    return path.resolve(strict=True)


def _identity(authority):
    from gateway.session_protected_admission import _source_identity
    return dict(profile_id=authority.profile_id, owner_instance=authority.instance_id, epoch=authority.epoch,
                source=_source_identity())


def _mapping(authority, policy):
    from hermes_cli.profiles import resolve_profile_env
    mapping = {name: str(Path(resolve_profile_env(name)).resolve(strict=True)) for name in
               (policy.implementer_profile, policy.reviewer_profile)}
    if (len(set(mapping.values())) != 2
            or authority.profile_id != mapping[policy.implementer_profile if policy.role == 'builder' else policy.reviewer_profile]):
        _deny()
    return mapping


def _snapshot(conn, policy):
    task = conn.execute('SELECT * FROM tasks WHERE id=?', (policy.task_id,)).fetchone()
    if task is None:
        _deny()
    return {k: task[k] for k in ('id','status','assignee','current_run_id','claim_lock','claim_expires','max_retries')}


def native_phase_bootstrap_expected(authority, *, policy: HeldPhasePolicy, controller_started_at: float, publication_identity: dict) -> dict:
    """Nonsecret scope for the existing OperatorAuthority, before first admission."""
    from hermes_cli.kanban_db_connect import connect
    _owner(authority)
    if type(policy) is not HeldPhasePolicy:
        _deny()
    if type(controller_started_at) not in (int, float) or not math.isfinite(controller_started_at):
        _deny()
    if (type(publication_identity) is not dict or set(publication_identity) != {'app_id','login'}
            or type(publication_identity['app_id']) is not int or publication_identity['app_id'] <= 0
            or type(publication_identity['login']) is not str or not publication_identity['login'].endswith('[bot]')):
        _deny()
    path = _path(authority, policy)
    with closing(connect(path)) as conn:
        task = _snapshot(conn, policy)
        if (task['status'] != 'blocked' or task['current_run_id'] is not None or task['claim_lock'] is not None
                or task['max_retries'] != 0 or task['assignee'] != policy.implementer_profile):
            _deny()
    return dict(action='protected_phase_bootstrap_v1', owner=_identity(authority), phase_policy=asdict(policy),
                owners=_mapping(authority, policy), db=str(path), card=task, controller_started_at=controller_started_at,
                publication_identity=_copy(publication_identity))


def _verify(verify, envelope, expected):
    approved = verify(envelope, expected)
    if type(approved) is not dict or not approved.get('approval_id') or any(approved.get(k) != v for k,v in expected.items()):
        _deny()
    try:
        expiry = datetime.fromisoformat(approved['expires_at'])
        if expiry.tzinfo is None or expiry <= datetime.now(UTC):
            _deny()
    except (ValueError, TypeError, KeyError):
        _deny()
    return approved


@dataclass(frozen=True)
class _HeldCard:
    policy: HeldPhasePolicy
    path: Path
    verify: object
    ledger: object
    started: float
    bootstrap: dict
    deadline: float


def install_held_card(authority, *, policy: HeldPhasePolicy, bootstrap_envelope: dict, verify,
                      ledger, controller_started_at: float, publication_identity: dict):
    """Install one card capability before recovery, with the SAME broker ledger.

    Ledger/start are explicit trusted bootstrap dependencies: the first claim
    precedes the first bridge, so guessing a new budget there would be unsafe.
    """
    if getattr(authority, '_held_card', None) is not None or not callable(verify):
        _deny()
    expected = native_phase_bootstrap_expected(authority, policy=policy, controller_started_at=controller_started_at,
                                              publication_identity=publication_identity)
    _verify(verify, bootstrap_envelope, expected)
    if not callable(getattr(ledger, 'check', None)):
        from gateway.session_protected_broker import BrokerRequestLedgerAdapter
        ledger = BrokerRequestLedgerAdapter(ledger, controller_started_at)
    deadline = time.monotonic() + max(0, controller_started_at + 600 - time.time())
    from gateway.session_card_owner import owned_card_authority
    for home in expected['owners'].values():
        peer = owned_card_authority(home)
        previous = getattr(peer, '_held_card', None) if peer is not None else None
        if type(previous) is _HeldCard and previous.policy.graph_digest == policy.graph_digest:
            if previous.started != controller_started_at or not _same_ledger(previous.ledger, ledger):
                _deny()
            deadline = min(deadline, previous.deadline)
    cap = _HeldCard(policy, Path(expected['db']), verify, ledger, controller_started_at, _copy(expected), deadline)
    _budget(cap, claim=True)
    authority._held_card = cap


def _budget(cap, *, claim):
    state = cap.ledger.check(dict(phase_policy=asdict(cap.policy)))
    if (type(state) is not dict or state.get('started_at') != cap.started or state.get('frozen') is not False
            or type(state.get('requests')) is not int or not 0 <= state['requests'] <= (11 if claim else 12)
            or not cap.started <= time.time() < cap.started + 600 or time.monotonic() >= cap.deadline):
        _deny()


def _cap(authority):
    _owner(authority)
    cap = getattr(authority, '_held_card', None)
    if type(cap) is not _HeldCard or _identity(authority) != cap.bootstrap['owner'] or _mapping(authority, cap.policy) != cap.bootstrap['owners']:
        _deny()
    with authority.db._read_ctx() as conn:
        if conn.execute('SELECT 1 FROM state_meta WHERE key=?', (_stop_key(cap),)).fetchone():
            _deny()
    return cap


def check_held_stop(authority):
    cap = getattr(authority, '_held_card', None)
    if type(cap) is not _HeldCard:
        _deny()
    _owner(authority)
    with authority.db._read_ctx() as conn:
        if conn.execute('SELECT 1 FROM state_meta WHERE key=?', (_stop_key(cap),)).fetchone():
            _deny()


def _stop_key(cap):
    return 'protected.phase.stop.v1.' + _digest(dict(graph=cap.policy.graph_digest,card=cap.policy.card_id))


def stop_held_card(authority, ref):
    # Called only AFTER the native authenticated interrupt/generation guard.
    from hermes_state_local import local_receipt
    cap = getattr(authority, '_held_card', None)
    saved = local_receipt(authority.db, ref.session_id)
    if type(cap) is not _HeldCard or saved is None:
        return
    context = json.loads(saved['policy'].get('kanban_json') or '{}')
    if context.get('task_id') != cap.policy.task_id or context.get('board') != cap.policy.board:
        return
    from hermes_state_runtime import _epoch
    def stop(conn):
        _epoch(conn, authority.epoch)
        conn.execute('INSERT OR IGNORE INTO state_meta(key,value) VALUES(?,?)',
            (_stop_key(cap), json.dumps(dict(epoch=authority.epoch,session_id=ref.session_id))))
    authority.db._execute_write(stop)


def _state(conn, cap):
    from hermes_cli import kanban_db as kb
    event = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='protected_phase' ORDER BY id DESC LIMIT 1", (cap.policy.task_id,)).fetchone()
    return (None, 0) if event is None else (json.loads(event['payload']), event['id'])


def _expected(authority, cap, conn, *, board, task_id, admission_id, action, published_head, publication):
    from hermes_cli import kanban_db as kb
    if board != cap.policy.board or task_id != cap.policy.task_id or action not in (
            'claim_builder','request_review','claim_reviewer','request_changes','claim_repair','approve'):
        _deny()
    _budget(cap, claim=action.startswith('claim_'))
    task = _snapshot(conn, cap.policy)
    state, sequence = _state(conn, cap)
    execution = None
    if action == 'claim_builder':
        if state is not None or cap.policy.role != 'builder' or admission_id is not None:
            _deny()
        if task != cap.bootstrap['card']:
            _deny()
    else:
        if state is None or task['status'] != 'blocked':
            _deny()
        provenance = state.get('binding', {}).get('phase_policy') or state['expected']['phase_policy']
        if any(provenance[k] != asdict(cap.policy)[k] for k in asdict(cap.policy) if k not in ('role','input_head')):
            _deny()
        if 'authorization' in state:
            _verify(cap.verify, state['authorization'], state['expected_authorization'])
        else:
            _verify(cap.verify, state['envelope'], state['expected'])
        if action in ('claim_reviewer','claim_repair'):
            required = 'review_held' if action == 'claim_reviewer' else 'repair_held'
            role = 'reviewer' if action == 'claim_reviewer' else 'builder'
            _predecessor_owner(cap, state)
            if (state['phase'] != required or cap.policy.role != role or admission_id is not None
                    or task['current_run_id'] is not None or task['claim_lock'] is not None):
                _deny()
        else:
            readback = _readback(authority, cap, admission_id, conn)
            execution = {k:readback[k] for k in ('native','receipt_sha256','transcript_sha256','result_sha256','export_sha256')}
            if state.get('receipt_sha256') != _digest(readback['receipt']):
                _deny()
            if action == 'request_review':
                if cap.policy.role != 'builder' or state['phase'] not in ('builder_held','repair_held'):
                    _deny()
                _publication(cap, publication, published_head, 'PUBLISHED')
            elif action in ('request_changes','approve'):
                verdict = 'APPROVE' if action == 'approve' else 'REQUEST_CHANGES'
                if cap.policy.role != 'reviewer' or state['phase'] != 'reviewer_held':
                    _deny()
                try:
                    observed = _observed_decision(readback['receipt']['result']['final_response'])
                except (ValueError, TypeError, KeyError):
                    _deny()
                if observed != {'verdict': verdict, 'head': readback['phase_policy']['input_head']} or published_head != observed['head']:
                    _deny()
                _publication(cap, publication, published_head, 'APPROVED' if action == 'approve' else 'CHANGES_REQUESTED')
            else:
                _deny()
    if action.startswith('claim_') and (published_head is not None or publication is not None):
        _deny()
    if not kb._parents_satisfied(conn, task_id):
        _deny()
    return dict(action='protected_native_phase_v1', phase_action=action, owner=_identity(authority),
        phase_policy=asdict(cap.policy), owners=cap.bootstrap['owners'], db=str(cap.path), card=task,
        admission_id=admission_id, phase_sequence=sequence, previous_sha256=_digest(state),
        published_head=published_head, publication=_copy(publication), controller_started_at=cap.started,
        execution=execution, publication_identity=cap.bootstrap['publication_identity'],
        input_head=published_head or (state['input_head'] if state else cap.policy.input_head),
        next_profile=cap.policy.reviewer_profile if action in ('request_review','claim_reviewer') else (
            cap.policy.implementer_profile if action != 'approve' else None))


PhaseAction = Literal['claim_builder','request_review','claim_reviewer','request_changes','claim_repair','approve']


def native_phase_expected(authority, *, board: str, task_id: str, admission_id: str | None,
                          action: PhaseAction, published_head: str | None, publication: dict | None) -> dict:
    from hermes_cli.kanban_db_connect import connect
    cap = _cap(authority)
    with closing(connect(cap.path)) as conn:
        return _expected(authority, cap, conn, board=board, task_id=task_id, admission_id=admission_id,
                         action=action, published_head=published_head, publication=publication)


def native_phase_commit(authority, *, envelope: dict) -> dict:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    cap = _cap(authority)
    if type(envelope) is not dict or type(envelope.get('payload')) is not dict:
        _deny()
    payload = envelope['payload']
    if payload.get('phase_action') in ('request_review','request_changes','approve'):
        return _transition(authority, cap, envelope)
    with closing(connect(cap.path)) as conn, kb.write_txn(conn):
        # Exactly the same signed operation returns its original context, even
        # after later phases. This lookup never issues another run or SDK claim.
        for row in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='protected_phase'", (cap.policy.task_id,)):
            recorded = json.loads(row[0])
            if recorded.get('envelope_sha256') == _digest(envelope):
                _verify(cap.verify, envelope, recorded['expected'])
                return recorded['response']
        expected = _expected(authority, cap, conn, board=cap.policy.board, task_id=cap.policy.task_id,
            admission_id=payload.get('admission_id'), action=payload.get('phase_action'),
            published_head=payload.get('published_head'), publication=payload.get('publication'))
        _verify(cap.verify, envelope, expected)
        action = expected['phase_action']
        if not action.startswith('claim_'):
            _deny()
        return _claim(authority, cap, conn, expected, envelope)


def _record(conn, cap, *, phase, expected, envelope, response, run_id, input_head):
    from hermes_cli import kanban_db as kb
    kb._append_event(conn, cap.policy.task_id, 'protected_phase', dict(phase=phase,
        expected=expected, envelope=_copy(envelope), envelope_sha256=_digest(envelope),
        response=response, input_head=input_head), run_id=run_id)


def _claim(authority, cap, conn, expected, envelope):
    from hermes_cli import kanban_db as kb
    action = expected['phase_action']
    prior, _ = _state(conn, cap)
    head = cap.policy.input_head if prior is None else prior['input_head']
    reviewer = action == 'claim_reviewer'
    assignee = cap.policy.reviewer_profile if reviewer else cap.policy.implementer_profile
    task = _snapshot(conn, cap.policy)
    if task['assignee'] != assignee or task['claim_lock'] is not None:
        _deny()
    now = int(time.time())
    run_id = kb._claim_and_open_run(conn, cap.policy.task_id, 'blocked', kb._claimer_id(),
        min(now + 600, int(cap.started + 600)), now, event_extra={'source_status':'review'} if reviewer else None)
    if run_id is None:
        _deny()
    task = kb.get_task(conn, cap.policy.task_id)
    context = dict(board=cap.policy.board, task_id=task.id, run_id=run_id, claim_lock=task.claim_lock, db=str(cap.path))
    _record(conn, cap, phase='reviewer_running' if reviewer else ('repair_running' if action == 'claim_repair' else 'builder_running'),
        expected=expected, envelope=envelope, response=context, run_id=run_id, input_head=head)
    return context


def _predecessor_owner(cap, state):
    from gateway.session_card_owner import owned_card_authority
    prior = state['expected']['owner']
    authority = owned_card_authority(prior['profile_id'])
    if authority is None:
        _deny()
    _cap(authority)
    if _identity(authority) != prior:
        _deny()
    peer = getattr(authority, '_held_card', None)
    if (type(peer) is not _HeldCard or peer.path != cap.path or peer.started != cap.started
            or peer.bootstrap['publication_identity'] != cap.bootstrap['publication_identity']
            or not _same_ledger(peer.ledger, cap.ledger)):
        _deny()


def _observed_decision(text: str) -> dict:
    def unique(pairs):
        result = {}
        for key,value in pairs:
            if key in result:
                _deny()
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=unique)
    if (type(value) is not dict or set(value) != {'verdict','head'}
            or value['verdict'] not in ('APPROVE','REQUEST_CHANGES')
            or type(value['head']) is not str or not re.fullmatch('[a-f0-9]{40}',value['head'])):
        _deny()
    return value


def _publication(cap, value, head, state):
    record_id = value.get('publication_id' if state == 'PUBLISHED' else 'review_id') if type(value) is dict else None
    if (type(head) is not str or not re.fullmatch('[a-f0-9]{40}', head)
            or type(value) is not dict or value.get('head') != head or value.get('state') != state
            or type(value.get('actor')) is not dict or value['actor'].get('type') != 'Bot'
            or type(value['actor'].get('app_id')) is not int
            or {k:value['actor'].get(k) for k in ('app_id','login')} != cap.bootstrap['publication_identity']
            or type(value['actor'].get('login')) is not str or not value['actor']['login'].endswith('[bot]')
            or type(record_id) is not int or record_id <= 0
            or type(value.get('delivery_signature')) is not str or not value['delivery_signature']):
        _deny()


def _same_ledger(left, right):
    from gateway.session_protected_broker import BrokerRequestLedgerAdapter
    if left is right:
        return True
    return (type(left) is BrokerRequestLedgerAdapter and type(right) is BrokerRequestLedgerAdapter
        and left.callbacks is right.callbacks and left.controller_started_at == right.controller_started_at)


def validate_bridge(bridge):
    """Called before provider acquisition; never derive policy from the model row."""
    from hermes_cli.kanban_db_connect import connect
    cap = _cap(bridge.authority)
    policy = asdict(bridge.phase_policy)
    if _same_ledger(bridge.ledger, cap.ledger):
        bridge.ledger = cap.ledger
    bridge._controller_deadline = min(bridge._controller_deadline, cap.deadline)
    if (any(policy[k] != asdict(cap.policy)[k] for k in policy if k != 'input_head')
            or bridge.controller_started_at != cap.started or bridge.ledger is not cap.ledger
            or bridge.verify != cap.verify or bridge.context['db'] != str(cap.path)
            or bridge.context['board'] != cap.policy.board or bridge.context['task_id'] != cap.policy.task_id
            or bridge.launch.role != cap.policy.role):
        _deny()
    with closing(connect(cap.path)) as conn:
        state, _ = _state(conn, cap)
        if state is None or state['phase'] not in ('builder_running','repair_running','reviewer_running'):
            _deny()
        if (policy['input_head'] != state['input_head'] or any(state['response'][k] != bridge.context[k]
                for k in ('board','task_id','run_id','claim_lock','db'))):
            _deny()


def _bridge(authority, admission_id):
    from gateway.session_protected_admission import NativeAdmissionBridge, NativeAdmissionBridgeFactory
    bridge = getattr(authority, '_protected_admission_bridge', None)
    if type(bridge) is NativeAdmissionBridgeFactory and bridge.authority is authority:
        bridge = bridge._bridges.get(admission_id)
    if (type(bridge) is not NativeAdmissionBridge or bridge.authority is not authority
            or bridge.row['admission_id'] != admission_id or bridge.phase_policy is None):
        _deny()
    return bridge


def _readback(authority, cap, admission_id, conn):
    from hermes_state_runtime import get_session_admission
    from gateway.session_protected_settlement import PREFIX
    bridge = _bridge(authority, admission_id)
    bridge.recheck_source()
    if bridge._binding() != bridge.binding or bridge._context() != bridge.context:
        _deny()
    runner = bridge._runner
    if runner is None or not runner._closed.is_set():
        _deny()
    container = runner.turn.proxy.worker.container
    if not container.absent() or runner.process.poll() is None or any(t.is_alive() for t in runner.threads):
        _deny()
    row = get_session_admission(authority.db, admission_id=admission_id)
    with authority.db._read_ctx() as native:
        saved = native.execute('SELECT value FROM state_meta WHERE key=?', (PREFIX + admission_id,)).fetchone()
        if saved is None:
            _deny()
        receipt = json.loads(saved[0])
        messages = authority.db.get_messages(bridge.ref.session_id)
        from hermes_state_runtime import _session
        if _session(native, bridge.ref.session_id)['runtime_generation'] != bridge.row['generation']:
            _deny()
    if (row is None or row['status'] != 'terminal' or row['outcome'] != 'completed'
            or row['owner_epoch'] != authority.epoch or row['generation'] != bridge.row['generation']
            or row['principal_id'] != bridge.row['principal_id'] or row['target_session_id'] != bridge.ref.session_id
            or row['payload'] != bridge.row['payload'] or receipt.get('binding') != bridge.binding
            or receipt.get('phase_policy') != asdict(bridge.phase_policy)):
        _deny()
    attested = runner.turn._native_owner._container_binding
    namespace = dict(attested, name=container.name, daemon=container.runtime.daemon, endpoint=container.runtime.endpoint)
    if receipt.get('namespace') != namespace or attested['cid'] != container.cid:
        _deny()
    # The finite receipt was committed together with the canonical transcript.
    observed = runner.turn._native_owner._observations.messages
    actual = [{k: m.get(k) for k in expected} for m, expected in zip(messages, observed)]
    if len(messages) != len(observed) or actual != observed or hashlib.sha256(json.dumps(actual,sort_keys=True).encode()).hexdigest() != receipt['transcript_sha256']:
        _deny()
    task = _snapshot(conn, cap.policy)
    run = conn.execute('SELECT * FROM task_runs WHERE id=? AND task_id=?',
        (bridge.context['run_id'],cap.policy.task_id)).fetchone()
    state, _ = _state(conn, cap)
    if state is None:
        _deny()
    if run is None or json.loads(run['metadata'] or '{}').get('protected_success_v1') != receipt:
        _deny()
    if task['status'] == 'done':
        if run['outcome'] != 'completed' or state['phase'] != 'done':
            _deny()
    elif (task['status'] == 'blocked' and task['current_run_id'] is None and task['claim_lock'] is None
            and state['response']['run_id'] == bridge.context['run_id']
            and (state['phase'],run['outcome']) in (('review_held','review_requested'),('repair_held','changes_requested'))):
        pass  # read the already-closed SAME predecessor run, never invent DONE
    elif (task['status'] != 'blocked' or task['current_run_id'] != bridge.context['run_id']
            or task['claim_lock'] != bridge.context['claim_lock'] or run['claim_lock'] != bridge.context['claim_lock']
            or run['status'] != 'running' or not task['claim_expires'] or task['claim_expires'] <= time.time()):
        _deny()
    return dict(version='protected_phase_readback_v1', phase=state['phase'], native=bridge.binding,
        card=dict(bridge.binding['card']), source=bridge.source, phase_policy=asdict(bridge.phase_policy), receipt=receipt,
        sdk_policy={k: getattr(bridge.policy, k) for k in ('provider', 'model', 'base_url', 'accounting')},
        receipt_sha256=_digest(receipt), result_sha256=_digest(receipt['result']), export_sha256=_digest(receipt['artifacts']),
        transcript_sha256=receipt['transcript_sha256'], artifacts=receipt['artifacts'],
        namespace_retired=True, namespace=dict(cid=container.cid,name=container.name,attestation=runner.turn._native_owner._container_binding))


def native_phase_readback(authority, *, admission_id: str) -> dict:
    from hermes_cli.kanban_db_connect import connect
    cap = _cap(authority)
    with closing(connect(cap.path)) as conn:
        return _readback(authority, cap, admission_id, conn)


def settle_held(runner, receipt, guard, native_fence):
    """Irreversible safety hold precedes the cross-WAL native receipt write.

    A native/board write fault leaves BLOCKED + the exact current claim. There
    is deliberately no refund, automatic retry, reclaim or inference replay.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    bridge = runner._phase_bridge
    cap = _cap(bridge.authority)
    context = bridge.context
    with closing(connect(cap.path)) as conn:
        with kb.write_txn(conn):
            task = _snapshot(conn, cap.policy)
            if (runner._closed.is_set() or runner.turn.proxy.worker.stop.is_set()
                    or task['status'] not in ('running','blocked')
                    or task['current_run_id'] != context['run_id'] or task['claim_lock'] != context['claim_lock']
                    or not task['claim_expires'] or task['claim_expires'] <= time.time()):
                _deny()
            if task['status'] == 'running':
                conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (cap.policy.task_id,))
                kb._append_event(conn, cap.policy.task_id, 'protected_hold_pending',
                    dict(admission_id=bridge.row['admission_id'], binding=bridge.binding),run_id=context['run_id'])
        with kb.write_txn(conn):
            state, _ = _state(conn, cap)
            phase = 'reviewer_held' if cap.policy.role == 'reviewer' else (
                'repair_held' if state['phase'] in ('repair_running','repair_held') else 'builder_held')
            existing = conn.execute('SELECT metadata FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
            metadata = json.loads(existing[0] or '{}') if existing else {}
            if metadata.get('protected_success_v1') == receipt:
                with bridge.authority.db._read_ctx() as native:
                    native_fence(native, terminal=True)
                return receipt
            guard(conn)
            metadata['protected_success_v1'] = receipt
            conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps(metadata,sort_keys=True),context['run_id']))
            kb._append_event(conn, cap.policy.task_id, 'protected_phase', dict(phase=phase,
                binding=bridge.binding, receipt_sha256=_digest(receipt),
                authorization=_copy(bridge.envelope), expected_authorization=bridge.expected_authorization(),
                input_head=bridge.phase_policy.input_head, response=context), run_id=context['run_id'])
    return receipt


class _Replay(Exception):
    def __init__(self, response):
        self.response = response


def _transition(authority, cap, envelope):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    payload = envelope['payload']
    action = payload['phase_action']
    response = {}
    expected = None
    def guard(conn):
        nonlocal expected
        for row in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='protected_phase'", (cap.policy.task_id,)):
            record = json.loads(row[0])
            if record.get('envelope_sha256') == _digest(envelope):
                _verify(cap.verify, envelope, record['expected'])
                raise _Replay(record['response'])
        _cap(authority)
        expected = _expected(authority, cap, conn, board=cap.policy.board,task_id=cap.policy.task_id,
            admission_id=payload.get('admission_id'), action=action, published_head=payload.get('published_head'),publication=payload.get('publication'))
        _verify(cap.verify, envelope, expected)
    def project(conn, run_id):
        phase = {'request_review':'review_held','request_changes':'repair_held','approve':'done'}[action]
        if action != 'approve':
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (cap.policy.task_id,))
        response.update(phase=phase, board=cap.policy.board, task_id=cap.policy.task_id, run_id=run_id)
        _record(conn, cap, phase=phase, expected=expected,envelope=envelope,response=response,
                run_id=run_id,input_head=payload['published_head'])
    bridge = _bridge(authority,payload.get('admission_id'))
    try:
        with closing(connect(cap.path)) as conn:
            # Let each canonical helper own BEGIN IMMEDIATE; do not nest it.
            if action == 'request_review':
                ok = kb.request_review(conn,cap.policy.task_id,summary='Protected exact-head publication',
                    reviewer=cap.policy.reviewer_profile,expected_run_id=bridge.context['run_id'],
                    _phase_guard=guard,_phase_project=project)
            elif action == 'request_changes':
                ok,_ = kb.request_changes(conn,cap.policy.task_id,reason='Protected observed REQUEST_CHANGES',
                    expected_run_id=bridge.context['run_id'],_phase_guard=guard,_phase_project=project)
            else:
                readback = native_phase_readback(authority,admission_id=payload['admission_id'])
                ok = kb.complete_task(conn,cap.policy.task_id,result='Protected verified App approval',
                    metadata={'protected_success_v1':readback['receipt']},expected_run_id=bridge.context['run_id'],
                    fire_lifecycle_hook=False,_completion_guard=guard,_completion_project=project)
            if not ok:
                _deny()
    except _Replay as replay:
        return replay.response
    return response
