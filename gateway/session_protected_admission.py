"""Owner-installed bridge from native persisted admission to protected execution.

No config switch, row JSON factory, generic RPC or replacement control plane.
The existing deterministic broker supplies its pinned operator verifier and
shared durable request ledger. Unknown authority/accounting is denied, not
converted to a successful native admission.
"""
from dataclasses import asdict
from datetime import datetime, UTC
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sqlite3
import time
from types import SimpleNamespace

from gateway.session_protected_native_sdk import NativeSDKPolicy, construct_native_sdk
from hermes_state_runtime import RuntimeStoreError


def _source_identity():
    root = Path(__file__).resolve().parents[1]
    def git(*args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(root), *args],
            timeout=5, stderr=subprocess.DEVNULL).decode().strip()
    if git('status', '--porcelain'):
        raise RuntimeStoreError('protected_source_not_immutable')
    return dict(commit=git('rev-parse', 'HEAD'), tree=git('rev-parse', 'HEAD^{tree}'))


class NativeAdmissionBridge:
    """A trusted owner capability, NEVER deserialized from a model/request row.

    ``verify(envelope, expected)`` is the deployed OperatorAuthority.verify (or
    equivalent native pinned authority), not a boolean supplied by the worker.
    ``ledger`` is the same owner broker ledger across all card attempts/roles.
    Its reserve/check/reconcile/freeze methods must transact durable state.
    """
    def __init__(self, *, authority, ref, row, launch, sdk_policy, envelope, verify, ledger,
                 resolve, artifact_names, controller_started_at, source_identity=None):
        from gateway.session_protected_docker import DockerLaunch
        if type(launch) is not DockerLaunch or type(sdk_policy) is not NativeSDKPolicy:
            raise RuntimeStoreError('protected_owner_bridge_required')
        if not callable(getattr(ledger, 'check', None)) and callable(getattr(ledger, 'snapshot', None)):
            from gateway.session_protected_broker import BrokerRequestLedgerAdapter
            ledger = BrokerRequestLedgerAdapter(ledger, controller_started_at)
        if not callable(verify) or not callable(resolve) or not all(
                callable(getattr(ledger, k, None)) for k in ('check', 'reserve', 'reconcile', 'freeze')):
            raise RuntimeStoreError('protected_owner_bridge_required')
        from gateway.session_protected_docker import checked_paths
        self.artifact_names = checked_paths(artifact_names)
        self.authority, self.ref, self.row = authority, ref, json.loads(json.dumps(row))
        self.launch, self.policy, self.envelope = launch, sdk_policy, envelope
        self.verify, self.ledger, self.resolve = verify, ledger, resolve
        if type(controller_started_at) not in (int, float) or not math.isfinite(controller_started_at):
            raise RuntimeStoreError('protected_controller_start_required')
        self.controller_started_at = controller_started_at
        # Pin once; wall-clock rollback and new SDK/channel objects cannot renew it.
        self._controller_deadline = time.monotonic() + max(0, controller_started_at + 600 - time.time())
        self._turn_deadline = None
        # Optional source identity is a trusted fixture/deployment manifest reader,
        # never an input accepted by production_plan or the native worker RPC.
        self._source_identity = source_identity or _source_identity
        self.source = self._source_identity()
        self._consumed = False
        self._runner = None
        self._first_provider_activity_at = None
        self.context = self._context()
        self.binding = self._binding()

    def _context(self):
        from hermes_state_local import local_receipt
        saved = local_receipt(self.authority.db, self.ref.session_id)
        if saved is None:
            raise RuntimeStoreError('protected_owner_policy_mismatch')
        try:
            return json.loads(saved['policy']['kanban_json'])
        except (ValueError, KeyError, TypeError):
            raise RuntimeStoreError('protected_owner_policy_mismatch') from None

    def _binding(self):
        return dict(profile_id=self.authority.profile_id, principal_id=self.row['principal_id'],
            session_id=self.ref.session_id, admission_id=self.row['admission_id'],
            generation=self.row['generation'], epoch=self.authority.epoch,
            owner_instance=self.authority.instance_id, source=self.source,
            image=self.launch.runtime.image, image_closure=self.launch.runtime.source,
            card={k: self.context[k] for k in ('board', 'task_id', 'run_id', 'claim_lock')},
            workspace=str(self.launch.workspace), role=self.launch.role,
            inputs=list(self.launch.paths), artifacts=list(self.artifact_names), sdk_policy=asdict(self.policy),
            input_sha256=hashlib.sha256(json.dumps(self.row['payload'], sort_keys=True, allow_nan=False).encode()).hexdigest())

    def expected_authorization(self):
        """Public non-secret exact scope for the existing operator signer."""
        return dict(action='protected_native_admission_v1', binding=self.binding,
            accounting='subscription_request_bound', max_requests=12, max_elapsed_seconds=600,
            controller_started_at=self.controller_started_at, hard_token_usd_guarantee=False)

    def budget_state(self):
        state = self.ledger.check(self.binding)
        now = time.time()
        if (type(state) is not dict or state.get('started_at') != self.controller_started_at
                or type(state.get('requests')) is not int or not 0 <= state['requests'] <= 12
                or state.get('frozen') is not False or not self.controller_started_at <= now < self.controller_started_at + 600
                or time.monotonic() >= self._controller_deadline):
            raise RuntimeStoreError('protected_owner_budget_unknown_or_exhausted')
        return state

    def check(self):
        approved = self._check_native()
        self.budget_state()
        if self._turn_deadline is not None:
            self._turn_deadline.remaining()
        return approved

    def _check_native(self):
        from gateway.session_protected_process import _saved_policy
        from gateway.session_protected_capabilities import NativeLifecycle
        from hermes_state_runtime import get_session_admission
        current = get_session_admission(self.authority.db, admission_id=self.row['admission_id'])
        if current is None or current['payload'] != self.row['payload']:
            raise RuntimeStoreError('protected_owner_payload_mismatch')
        if (self._binding() != self.binding
                or self._context() != self.context or self.policy.session_id != self.ref.session_id):
            raise RuntimeStoreError('protected_owner_policy_mismatch')
        _saved_policy(self.authority, self.ref, self.row, self.launch, self.row['principal_id'],
            self.context, SimpleNamespace(model=self.policy.model, provider_id=self.policy.provider))
        probe = NativeLifecycle.__new__(NativeLifecycle)
        probe._context = self.context
        probe._card_check()  # canonical actual current card/run/claim; no synthetic attestation
        self._pilot_card_policy()
        approved = self.verify(self.envelope, self.expected_authorization())
        # Do not trust a loose boolean or digest masquerading as signed approval.
        if (type(approved) is not dict or not approved.get('approval_id')
                or any(approved.get(k) != v for k, v in self.expected_authorization().items())):
            raise RuntimeStoreError('protected_signed_owner_policy_required')
        try:
            expiry = datetime.fromisoformat(approved['expires_at'])
            if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise RuntimeStoreError('protected_signed_owner_policy_expired') from None
        self.policy.validate()  # strict current subscriptions still deny before credentials
        return approved

    def _pilot_card_policy(self):
        path = Path(self.context['db']).resolve(strict=True)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.2)) as conn:
            conn.row_factory = sqlite3.Row
            task = conn.execute('SELECT max_retries,max_runtime_seconds,completion_contract FROM tasks WHERE id=?',
                (self.context['task_id'],)).fetchone()
        if task is None or type(task['max_retries']) is not int or task['max_retries'] != 0:
            raise RuntimeStoreError('protected_native_retry_policy_denied')
        return dict(task)

    def provider(self):
        self.recheck_source()
        self.check()
        from agent.secret_scope import current_secret_scope_home
        if current_secret_scope_home() != self.authority.profile_id:
            raise RuntimeStoreError('protected_native_auth_profile_unbound')
        self.verify_runtime_image()
        bridge = self
        class GuardedLedger:
            def remaining_seconds(self):
                bridge.check()
                if bridge._turn_deadline is None:
                    raise RuntimeStoreError('protected_pilot_deadline_required')
                return min(bridge._turn_deadline.remaining(), bridge._controller_deadline - time.monotonic())
            def reserve(self, binding):
                bridge.check()
                before = bridge.budget_state()['requests']
                if before >= 12:
                    raise RuntimeStoreError('protected_request_limit')
                receipt = bridge.ledger.reserve(dict(binding, native_binding=bridge.binding))
                try:
                    if receipt is None or bridge.budget_state()['requests'] != before + 1:
                        raise RuntimeStoreError('protected_request_reservation_unknown')
                except BaseException:
                    bridge.ledger.freeze(receipt, 'reservation_unknown')
                    raise
                return receipt
            def reconcile(self, receipt, usage):
                bridge.check()
                return bridge.ledger.reconcile(receipt, usage)
            def freeze(self, receipt, reason):
                return bridge.ledger.freeze(receipt, reason)
        if self._first_provider_activity_at is None:
            self._first_provider_activity_at = time.time()
        selected = construct_native_sdk(self.policy, resolve=self.resolve, ledger=GuardedLedger())
        selected._admission_guard = self.check
        selected._protected_deadline = self._turn_deadline
        return selected

    def recheck_source(self):
        """Recheck at construction, provider acquisition and successful settlement.

        Host source is trusted and immutable; credentialless guests cannot write
        it. Avoid Git subprocesses on every tool/lifecycle fence within a run.
        """
        if self._source_identity() != self.source:
            raise RuntimeStoreError('protected_owner_policy_mismatch')

    def verify_runtime_image(self):
        """Real owner manifest + trusted daemon, never a guest-supplied attestation."""
        from gateway.session_protected_docker import CLOSURE
        runtime = self.launch.runtime
        manifest = json.loads((runtime.private / 'closure.json').read_text(encoding='utf-8-sig'))
        closure = manifest.pop('closure')
        if (closure != runtime.source or hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest() != closure
                or set(manifest['files']) != set(CLOSURE)):
            raise RuntimeStoreError('protected_runtime_source_mismatch')
        if 'commit' in self.source and (manifest['revision'] != self.source['commit']
                or manifest['tree'] != self.source['tree'] or manifest['dirty'] is not False):
            raise RuntimeStoreError('protected_runtime_source_mismatch')
        root = Path(__file__).resolve().parents[1]
        if any(hashlib.sha256((root / name).read_bytes()).hexdigest() != manifest['files'][name] for name in CLOSURE):
            raise RuntimeStoreError('protected_runtime_source_mismatch')
        runtime.verify_image()

    def readback(self, request_binding):
        """Authoritative owner-only observation for the broker's protected_attempt.

        Do NOT call the broker ledger from here: its reserve transaction is the
        caller. No generic RPC, cached JSON receipt or model assertion can replace
        this exact live native authority/process/daemon readback.
        """
        expected = dict(provider=self.policy.provider, model=self.policy.model,
            session_id=self.policy.session_id, base_url=self.policy.base_url,
            accounting=self.policy.accounting, native_binding=self.binding)
        if type(request_binding) is not dict or request_binding != expected:
            raise RuntimeStoreError('protected_broker_binding_mismatch')
        self._check_native()
        runner = self._runner
        if (runner is None or runner._closed.is_set() or runner.turn.proxy.worker.stop.is_set()
                or self._first_provider_activity_at is None):
            raise RuntimeStoreError('protected_native_attempt_not_running')
        owner = runner.turn._native_owner
        owner._check_native()
        container = runner.turn.proxy.worker.container
        from gateway.session_protected_docker import DockerContainer
        if type(container) is not DockerContainer:
            raise RuntimeStoreError('protected_native_vm_required')
        runtime = container.runtime
        if runtime.json(['info', '--format', '{{json .ID}}']) != runtime.daemon:
            raise RuntimeStoreError('protected_native_daemon_changed')
        attestation = container.attest()
        if attestation != owner._container_binding:
            raise RuntimeStoreError('protected_container_binding_mismatch')
        inspected = container.inspect()
        security = {k: inspected['HostConfig'][k] for k in ('ReadonlyRootfs', 'CapDrop', 'CapAdd', 'Privileged',
            'Binds', 'NetworkMode', 'PidMode', 'IpcMode', 'CgroupnsMode', 'PidsLimit', 'Memory', 'MemorySwap',
            'NanoCpus', 'SecurityOpt', 'Tmpfs', 'LogConfig', 'Ulimits')}
        from hermes_constants import profile_name_for_home
        scope = owner._scope
        return dict(version='protected_native_readback_v1', profile=profile_name_for_home(self.authority.profile_id) or 'default',
            provider=self.policy.provider, model=self.policy.model, source=self.source,
            first_provider_activity_at=self._first_provider_activity_at,
            native=dict(admission_id=self.row['admission_id'], principal_id=self.row['principal_id'],
                owner_instance=self.authority.instance_id, **{k: scope[k] for k in
                    ('profile_id', 'session_id', 'execution_id', 'generation', 'epoch', 'pid', 'birth')}),
            card={k: self.context[k] for k in ('board', 'task_id', 'run_id', 'claim_lock')} | self._pilot_card_policy(),
            container=dict(attestation, daemon=runtime.daemon, endpoint=runtime.endpoint,
                name=container.name, role=container.role, protocol='sentinel-only-v1',
                security=security, mounts=inspected['Mounts']))

    async def execute(self, text):
        """One exact native run. Failed projection requires explicit receipt recovery.

        Never rerun inference or hand off a card after an ambiguous cross-WAL
        write. The exact failed runner remains owner-reachable for diagnostics.
        """
        from gateway.session_protected_process import protected_process_scope
        if self._consumed:
            raise RuntimeStoreError('protected_admission_already_consumed')
        if type(text) is not str or text != self.row['payload'].get('text'):
            raise RuntimeStoreError('protected_owner_payload_mismatch')
        self.check()
        self._consumed = True
        if self.policy.pilot_timing is not None:
            from gateway.session_protected_timing import TurnDeadline
            self._turn_deadline = TurnDeadline(self.policy.pilot_timing, self._controller_deadline)
            # Owner-only signed selection, not DockerLaunch/config/model input.
            if getattr(self.launch, '_pilot_deadline', None) is not None:
                raise RuntimeStoreError('protected_pilot_deadline_already_bound')
            self.launch._pilot_deadline = self._turn_deadline
        provider = self.provider()
        async with protected_process_scope(self.authority, self.ref, self.row, self.launch,
                principal_id=self.row['principal_id'], context=self.context, provider=provider) as runner:
            self._runner = runner
            result = await runner.run_conversation(text)
            # Draining the actual SDK is a precondition of native success, not
            # cleanup after the durable completion receipt has already committed.
            try:
                await provider.close()
            except BaseException:
                self.ledger.freeze(None, 'provider_close_failed_or_cancelled')
                raise
            self.recheck_source()
            self.check()
            receipt = await runner.settle_success(self.artifact_names)
            self.authority.__dict__.setdefault('_protected_completion_receipts', {})[self.row['admission_id']] = receipt
            return result['final_response']


class NativeAdmissionBridgeFactory:
    """Trusted bootstrap-only builder, never a JSON/plugin factory.

    Install this exact capability on ``authority._protected_admission_bridge``.
    ``build(ref, canonical_row)`` is trusted owner code mapping the real card to
    a signed bridge; it must not load a path/command/module from the request.
    Failed builds remain consumed; the bounded pilot holds at most 12 entries.
    """
    def __init__(self, *, authority, build):
        if not callable(build):
            raise RuntimeStoreError('protected_owner_bridge_required')
        self.authority, self._build, self._bridges = authority, build, {}

    def resolve(self, ref, row):
        from hermes_state_runtime import get_session_admission
        saved = get_session_admission(self.authority.db, admission_id=row.get('admission_id'))
        keys = ('admission_id', 'generation', 'principal_id', 'payload', 'target_session_id')
        if (saved is None or saved['status'] != 'started' or saved['owner_epoch'] != self.authority.epoch
                or saved['target_session_id'] != ref.session_id
                or any(saved.get(k) != row.get(k) for k in keys)):
            raise RuntimeStoreError('protected_owner_bridge_required')
        key = saved['admission_id']
        if key not in self._bridges:
            if len(self._bridges) >= 12:
                raise RuntimeStoreError('protected_owner_bridge_required')
            self._bridges[key] = None  # no repeated builder side effects after failure
            bridge = self._build(ref, json.loads(json.dumps(saved)))
            if type(bridge) is not NativeAdmissionBridge or bridge.authority is not self.authority:
                raise RuntimeStoreError('protected_owner_bridge_required')
            self._bridges[key] = bridge
        return self._bridges[key]


def production_plan(authority, ref, row):
    bridge = getattr(authority, '_protected_admission_bridge', None)
    if type(bridge) is NativeAdmissionBridgeFactory and bridge.authority is authority:
        bridge = bridge.resolve(ref, row)
    if (type(bridge) is not NativeAdmissionBridge or bridge.authority is not authority
            or bridge.ref.session_id != ref.session_id or bridge.row['admission_id'] != row.get('admission_id')
            or bridge.row['generation'] != row.get('generation') or bridge.row['principal_id'] != row.get('principal_id')
            or bridge.row['payload'] != row.get('payload')):
        raise RuntimeStoreError('protected_owner_bridge_required')
    bridge.check()
    return bridge


def native_protected_readback(authority, request_binding):
    """Trusted canonical-owner callback; not an unauthenticated JSON endpoint."""
    bridge = getattr(authority, '_protected_admission_bridge', None)
    if type(bridge) is NativeAdmissionBridgeFactory and bridge.authority is authority:
        native = request_binding.get('native_binding') if type(request_binding) is dict else None
        bridge = bridge._bridges.get(native.get('admission_id')) if type(native) is dict else None
    if type(bridge) is not NativeAdmissionBridge or bridge.authority is not authority:
        raise RuntimeStoreError('protected_owner_bridge_required')
    return bridge.readback(request_binding)
