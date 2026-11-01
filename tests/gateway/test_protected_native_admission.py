"""Trusted-native bridge: real scratch authority/board, signed fixtures, no live auth."""
import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timedelta, UTC
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

# Ordinary owner bootstrap before home-I/O guards; never bootstrapped in a guest.
import gateway.session_protected_scope
from tests.gateway.test_protected_native_sdk import serve_fixture


@pytest.fixture(autouse=True)
def controlled_owner_secret_scope(tmp_path):
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    token = set_secret_scope({}, profile_home=str(tmp_path))  # controlled dummy scope, never auth discovery
    try:
        yield
    finally:
        reset_secret_scope(token)


@pytest.fixture(scope='session')
def docker_runtime(tmp_path_factory):
    from tests.gateway.docker_boundary_config import build_boundary_runtime
    return build_boundary_runtime(tmp_path_factory.mktemp('native-sdk-docker-runtime'))


def test_untrusted_row_cannot_enable_native_bridge():
    from gateway.session_protected_tools import production_plan
    with pytest.raises(ValueError, match='protected_owner_bridge_required'):
        production_plan(SimpleNamespace(), SimpleNamespace(session_id='untrusted'),
                        dict(protected_tools_v1={'enabled': True, 'accounting': 'subscription_request_bound'}))


def test_owner_factory_rejects_serialized_or_callable_authority():
    from gateway.session_protected_admission import NativeAdmissionBridgeFactory, production_plan
    authority = SimpleNamespace()
    with pytest.raises(ValueError, match='protected_owner_bridge_required'):
        NativeAdmissionBridgeFactory(authority=authority, build={'enabled': True})
    authority._protected_admission_bridge = lambda *args: True
    with pytest.raises(ValueError, match='protected_owner_bridge_required'):
        production_plan(authority, SimpleNamespace(session_id='untrusted'), {})


class SignedFixture:
    """Controlled RSA fixture, same canonical RSA/SHA256 scope as OperatorAuthority.

    Not a real Jack approval and never installed. The production verifier remains
    the broker's pinned OperatorAuthority.verify, passed only by trusted owner.
    """
    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    def sign(self, expected):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        payload = dict(expected, approval_id='CONTROLLED_NOT_OWNER_APPROVAL',
            expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat())
        signature = self.key.sign(json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode(),
            padding.PKCS1v15(), hashes.SHA256())
        return dict(payload=payload, signature=base64.b64encode(signature).decode(), key_id='CONTROLLED')
    def verify(self, envelope, expected):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        if (type(envelope) is not dict or set(envelope) != {'payload', 'signature', 'key_id'}
                or envelope['key_id'] != 'CONTROLLED' or any(envelope['payload'].get(k) != v for k, v in expected.items())):
            raise RuntimeError('controlled_signature_scope_denied')
        try:
            self.key.public_key().verify(base64.b64decode(envelope['signature'], validate=True),
                json.dumps(envelope['payload'], sort_keys=True, separators=(',', ':'), allow_nan=False).encode(),
                padding.PKCS1v15(), hashes.SHA256())
        except Exception:
            raise RuntimeError('controlled_signature_denied') from None
        return envelope['payload']


class DurableFixtureLedger:
    """Controlled durable ledger; production must supply the shared broker ledger."""
    def __init__(self, path):
        self.path = path
        self.observe, self.proofs, self.bindings = None, [], []
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, state TEXT, usage TEXT)')
            conn.execute('CREATE TABLE IF NOT EXISTS controller(started REAL NOT NULL, frozen INTEGER NOT NULL)')
            if conn.execute('SELECT count(*) FROM controller').fetchone()[0] == 0:
                import time
                conn.execute('INSERT INTO controller VALUES(?,0)', (time.time(),))
    def check(self, binding):
        import time
        with sqlite3.connect(self.path) as conn:
            started, frozen = conn.execute('SELECT started,frozen FROM controller').fetchone()
            requests = conn.execute('SELECT count(*) FROM attempts').fetchone()[0]
        if frozen or time.time() - started >= 600:
            raise RuntimeError('controlled_ledger_expired_or_frozen')
        return dict(started_at=started, frozen=bool(frozen), requests=requests)
    def reserve(self, binding):
        self.check(binding)
        if self.observe is not None:
            self.bindings.append(binding)
            self.proofs.append(self.observe(binding))
        with sqlite3.connect(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            count = conn.execute('SELECT count(*) FROM attempts').fetchone()[0]
            if count >= 12:
                raise RuntimeError('controlled_cumulative_limit')
            return conn.execute("INSERT INTO attempts(state) VALUES('reserved')").lastrowid
    def reconcile(self, receipt, usage):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE attempts SET state='reported',usage=? WHERE id=?", (json.dumps(usage), receipt))
    def freeze(self, receipt, reason):
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE controller SET frozen=1')
            conn.execute("UPDATE attempts SET state='unknown' WHERE id=?", (receipt,))


class BrokerShapeLedger:
    """Merged PilotRequestLedger protocol over actual controlled fixture storage."""
    def __init__(self, ledger):
        self.ledger = ledger
    def snapshot(self):
        import time
        value = self.ledger.check({})
        return dict(mode='subscription_request_bound', request_count=value['requests'],
            elapsed_seconds=time.time() - value['started_at'], usd_guarantee=False)
    def reserve(self, binding):
        return self.ledger.reserve(binding)
    def reconcile(self, receipt, usage):
        return self.ledger.reconcile(receipt, usage)
    def freeze(self, receipt, reason):
        return self.ledger.freeze(receipt, reason)


def fixture_source():
    """Actual source-byte identity for controlled dirty-tree tests, not Git attestation."""
    root = Path(__file__).resolve().parents[2]
    files = ['gateway/session_protected_native_sdk.py', 'gateway/session_protected_admission.py']
    return {'controlled_source_sha256': hashlib.sha256(b''.join((root / f).read_bytes() for f in files)).hexdigest()}


def fixture_bridge(authority, row, launch, policy, ledger):
    from gateway.session_protected_admission import NativeAdmissionBridge, _source_identity
    from hermes_state_runtime import RuntimeStoreError
    # Mutable TDD runs use an explicitly labelled source-byte fixture. On the
    # immutable candidate/hosted checkout exercise the actual production reader
    # and exact clean commit/tree/image-manifest checks, with no injected identity.
    try:
        _source_identity()
    except RuntimeStoreError as error:
        if str(error) != 'protected_source_not_immutable':
            raise
        private = getattr(launch.runtime, 'private', None)
        if private is not None and json.loads((private / 'closure.json').read_text())['dirty'] is False:
            raise AssertionError('immutable Docker run MUST use clean production commit/tree reader') from error
        source_identity = fixture_source
    else:
        source_identity = None
    signer = SignedFixture()
    resolve_calls = []
    def resolve():
        resolve_calls.append(True)
        return dict(provider=policy.provider, base_url=policy.base_url,
            api_key='CONTROLLED_DUMMY_NOT_A_SECRET', api_mode='anthropic_messages' if policy.provider == 'anthropic' else 'codex_responses')
    bridge = NativeAdmissionBridge(authority=authority, ref=SimpleNamespace(session_id=row['target_session_id']),
        row=row, launch=launch, sdk_policy=policy, envelope=None, verify=signer.verify,
        ledger=ledger, resolve=resolve, artifact_names=('allowed.txt',), source_identity=source_identity,
        controller_started_at=getattr(ledger, 'ledger', ledger).check({})['started_at'])
    bridge.envelope = signer.sign(bridge.expected_authorization())
    if source_identity is None:
        assert set(bridge.source) == {'commit', 'tree'}
    return bridge, signer, resolve_calls


def test_owner_factory_uses_canonical_row_once_and_rechecks_source(tmp_path, monkeypatch):
    from gateway.session_protected_admission import NativeAdmissionBridgeFactory, production_plan
    from gateway.session_protected_native_sdk import NativeSDKPolicy
    from gateway.session_protected_docker import DockerLaunch, DockerRuntime
    from tests.gateway.test_protected_conversation import native_claim
    authority, _context, row = native_claim(tmp_path, monkeypatch, persisted=True,
        provider='anthropic', model='controlled-model', text='CONTROLLED', max_retries=0)
    runtime = DockerRuntime.__new__(DockerRuntime)
    runtime.image, runtime.source = 'sha256:' + '0' * 64, '0' * 64
    launch = DockerLaunch(tmp_path.resolve(), runtime, tmp_path / 'leases', ('allowed.txt',), 'builder')
    policy = NativeSDKPolicy(provider='anthropic', model='controlled-model', session_id=row['target_session_id'],
        base_url='https://api.anthropic.com', accounting='subscription_request_bound')
    calls, reads = [], []
    def build(ref, saved):
        calls.append(saved)
        bridge, _signer, _resolved = fixture_bridge(authority, saved, launch, policy, DurableFixtureLedger(tmp_path / 'ledger.db'))
        identity = bridge._source_identity
        bridge._source_identity = lambda: reads.append(True) or identity()
        return bridge
    factory = NativeAdmissionBridgeFactory(authority=authority, build=build)
    authority._protected_admission_bridge = factory
    ref = SimpleNamespace(session_id=row['target_session_id'])
    try:
        forged = dict(row, payload={'text': 'FORGED'})
        with pytest.raises(ValueError, match='protected_owner_bridge_required'):
            production_plan(authority, ref, forged)
        assert not calls
        bridge = production_plan(authority, ref, row)
        assert production_plan(authority, ref, row) is bridge and len(calls) == 1
        for _ in range(20):
            bridge.check()
        assert not reads  # no Git subprocess on the inference/tool hot path
        bridge._source_identity = lambda: {'changed': True}
        with pytest.raises(ValueError, match='protected_owner_policy_mismatch'):
            bridge.provider()  # strong source recheck BEFORE credentials
    finally:
        authority.db.close()


@pytest.mark.parametrize('fault', ['strict', 'unsigned', 'signature', 'expired', 'principal', 'session', 'epoch', 'claim', 'model', 'endpoint', 'image', 'ledger', 'payload', 'retry'])
def test_bound_native_policy_refuses_before_auth(tmp_path, monkeypatch, fault):
    from gateway.session_protected_native_sdk import NativeSDKPolicy
    from gateway.session_protected_docker import DockerLaunch, DockerRuntime
    from tests.gateway.test_protected_conversation import native_claim
    authority, _context, row = native_claim(tmp_path, monkeypatch, persisted=True,
        provider='anthropic', model='controlled-model', max_retries=1 if fault == 'retry' else 0)
    # Preflight-only placeholder metadata: no Docker attestation/process is claimed.
    runtime = DockerRuntime.__new__(DockerRuntime)
    runtime.image, runtime.source = 'sha256:' + '0' * 64, '0' * 64
    launch = DockerLaunch(tmp_path.resolve(), runtime, tmp_path / 'leases', ('allowed.txt',), 'builder')
    policy = NativeSDKPolicy(provider='anthropic', model='controlled-model', session_id=row['target_session_id'],
        base_url='https://api.anthropic.com', accounting='strict' if fault == 'strict' else 'subscription_request_bound')
    bridge, _signer, resolved = fixture_bridge(authority, row, launch, policy, DurableFixtureLedger(tmp_path / 'ledger.db'))
    authority._protected_admission_bridge = bridge
    if fault == 'unsigned': bridge.envelope = None  # health: allow HX011 -- explicit adversarial fixture mutations, not production dispatch
    elif fault == 'signature': bridge.envelope['signature'] = base64.b64encode(b'INVALID').decode()
    elif fault == 'expired':
        bridge.envelope['payload']['expires_at'] = '2000-01-01T00:00:00+00:00'
    elif fault == 'principal': bridge.row['principal_id'] = 'intruder'
    elif fault == 'session': bridge.ref = SimpleNamespace(session_id='wrong-session')
    elif fault == 'epoch': authority.epoch += 1
    elif fault == 'claim': bridge.context['claim_lock'] = 'stale'
    elif fault == 'model': bridge.policy = replace(policy, model='wrong-model')
    elif fault == 'endpoint': bridge.policy = replace(policy, base_url='https://evil.invalid')
    elif fault == 'image': runtime.image = 'sha256:' + '1' * 64
    elif fault == 'ledger': bridge.ledger.check = lambda binding: None
    elif fault == 'payload': row['payload']['text'] = 'UNSIGNED_TEXT'
    from gateway.session_protected_tools import production_plan
    try:
        with pytest.raises((RuntimeError, ValueError, KeyError)):
            production_plan(authority, SimpleNamespace(session_id=row['target_session_id']), row)
        assert not resolved
        with sqlite3.connect(tmp_path / 'ledger.db') as conn:
            assert conn.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0
    finally:
        authority.db.close()


@pytest.mark.parametrize('provider', ['anthropic', 'openai-codex'])
@pytest.mark.parametrize('delay', [None, 'headers', 'body', 'closefault'])
def test_real_docker_sdk_native_bridge(tmp_path, monkeypatch, docker_runtime, provider, delay):
    from gateway.session_protected_native_sdk import NativeSDKPolicy
    from gateway.session_protected_docker import DockerLaunch
    from tests.gateway.test_protected_conversation import native_claim
    from hermes_state_runtime import get_session_admission
    from gateway.session_managed_worker import execute_managed
    authority, _context, row = native_claim(tmp_path, monkeypatch, persisted=True,
        provider=provider, model='controlled-model', text='CONTROLLED_SDK_DOCKER_BRIDGE', max_retries=0)
    (tmp_path / 'allowed.txt').write_text('CONTROLLED_ARTIFACT\n')
    async def run():
        state = dict(requests=[], received=asyncio.Event(), disconnected=asyncio.Event(), tool_roundtrip=delay is None)
        peers = set()
        def connected(reader, writer):
            peers.add(asyncio.create_task(serve_fixture(reader, writer, state=state, provider=provider, delay=delay)))
        server = await asyncio.start_server(connected, '127.0.0.1', 0)
        policy = NativeSDKPolicy(provider=provider, model='controlled-model', session_id=row['target_session_id'],
            base_url=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}', accounting='subscription_request_bound')
        launch = DockerLaunch(tmp_path.resolve(), docker_runtime, tmp_path / 'leases', ('allowed.txt',), 'builder')
        ledger = DurableFixtureLedger(tmp_path / 'ledger.db')
        callbacks = BrokerShapeLedger(ledger) if provider == 'openai-codex' else ledger
        bridge, _signer, resolved = fixture_bridge(authority, row, launch, policy, callbacks)
        authority._protected_admission_bridge = bridge
        ledger.observe = bridge.readback  # actual authoritative native + Docker observation, not JSON acceptance
        if delay == 'closefault':
            import httpx
            actual_close = httpx.AsyncClient.aclose
            async def broken_close(client):
                await actual_close(client)
                raise OSError('CONTROLLED_DUMMY_SECRET_CLOSE_FAULT')
            monkeypatch.setattr(httpx.AsyncClient, 'aclose', broken_close)
        # Real native execute entry, not a direct SelectedAsyncProvider fixture.
        task = asyncio.create_task(execute_managed(authority, bridge.ref, row, None))
        try:
            await asyncio.wait_for(state['received'].wait(), 12)
            if delay in ('headers', 'body'):
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await asyncio.wait_for(state['disconnected'].wait(), 2)
                assert get_session_admission(authority.db, admission_id=row['admission_id'])['outcome'] != 'completed'
                assert len(authority.db.get_messages(row['target_session_id'])) <= 1
                assert not getattr(authority, '_protected_completion_receipts', {})
            elif delay == 'closefault':
                with pytest.raises(RuntimeError, match='provider_close_failed') as error:
                    await task
                assert 'CONTROLLED_DUMMY' not in str(error.value)
                assert get_session_admission(authority.db, admission_id=row['admission_id'])['outcome'] != 'completed'
                assert not getattr(authority, '_protected_completion_receipts', {})
            else:
                assert await asyncio.wait_for(task, 12) == 'CONTROLLED_SDK_DONE'
                receipt = authority._protected_completion_receipts[row['admission_id']]
                assert receipt['outcome'] == 'completed' and receipt['transcript_sha256']
                assert base64.b64decode(receipt['artifacts']['allowed.txt']['body']) == b'CONTROLLED_ARTIFACT\n'
                assert len(authority.db.get_messages(row['target_session_id'])) == 4
                admitted = get_session_admission(authority.db, admission_id=row['admission_id'])
                assert admitted['status'] == 'terminal' and admitted['outcome'] == 'completed'
                with authority.db._read_ctx() as conn:
                    stored = conn.execute('SELECT value FROM state_meta WHERE key=?',
                        ('protected.success.v1.' + row['admission_id'],)).fetchone()
                    assert json.loads(stored[0]) == receipt
                board_uri = Path(bridge.context['db']).as_uri() + '?mode=ro'
                with sqlite3.connect(board_uri, uri=True) as conn:
                    assert conn.execute('SELECT status FROM tasks WHERE id=?', (bridge.context['task_id'],)).fetchone()[0] == 'done'
                    run = conn.execute('SELECT status,outcome,metadata FROM task_runs WHERE id=?',
                        (bridge.context['run_id'],)).fetchone()
                    assert run[:2] == ('done', 'completed')
                    assert json.loads(run[2])['protected_success_v1'] == receipt
            assert len(resolved) == 1
            assert len(state['requests']) == (2 if delay is None else 1)
            assert len(ledger.proofs) == len(state['requests'])
            for proof in ledger.proofs:
                assert proof['version'] == 'protected_native_readback_v1'
                assert proof['native']['admission_id'] == row['admission_id']
                assert proof['container']['cid'] == bridge._runner.turn.proxy.worker.container.cid
                assert proof['container']['daemon'] == docker_runtime.daemon
                assert proof['container']['image'] == docker_runtime.image
                assert proof['container']['security']['NetworkMode'] == 'none'
                assert proof['container']['mounts'] == []
                assert 'CONTROLLED_DUMMY' not in json.dumps(proof)
            assert bridge._runner.turn.proxy.worker.container.absent()
            assert authority._managed_workers == {}
            assert not bridge._runner.provider._tasks
            assert not any(thread.is_alive() for thread in bridge._runner.threads)
            assert bridge._runner.process.poll() is not None
            docker_runtime.record('native-readbacks-' + row['admission_id'],
                dict(source=bridge.source, proofs=ledger.proofs, provider=provider,
                    namespace_absent=bridge._runner.turn.proxy.worker.container.absent()))
            with pytest.raises((RuntimeError, ValueError)):
                bridge.readback(ledger.bindings[0])
            with pytest.raises(ValueError, match='already_consumed'):
                await bridge.execute('DO_NOT_RERUN')
            with sqlite3.connect(tmp_path / 'ledger.db') as conn:
                assert conn.execute('SELECT count(*) FROM attempts').fetchone()[0] == (2 if delay is None else 1)
                request_state = conn.execute('SELECT state FROM attempts').fetchone()[0]
                if delay == 'closefault':
                    assert request_state in ('unknown', 'reported')
                    assert conn.execute('SELECT frozen FROM controller').fetchone()[0] == 1
                else:
                    assert request_state == ('unknown' if delay in ('headers', 'body') else 'reported')
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            server.close()
            await server.wait_closed()
            await asyncio.wait_for(asyncio.gather(*peers), 2)
    try:
        asyncio.run(run())
    finally:
        authority.db.close()


@pytest.mark.parametrize('provider', ['anthropic', 'openai-codex'])
def test_canonical_drain_factory_completes_once_without_fifo_pause(tmp_path, monkeypatch, docker_runtime, provider):
    from gateway.session_protected_admission import NativeAdmissionBridgeFactory, native_protected_readback
    from gateway.session_protected_native_sdk import NativeSDKPolicy
    from gateway.session_protected_docker import DockerLaunch
    from gateway.session_local_recovery import restore_local_session
    from hermes_state_runtime import admit_session_input, get_session_admission
    from tests.gateway.test_protected_conversation import native_claim
    authority, _context, row = native_claim(tmp_path, monkeypatch, persisted=True,
        provider=provider, model='controlled-model', text='CONTROLLED_DRAIN', max_retries=0, claim=False)
    (tmp_path / 'allowed.txt').write_text('CONTROLLED_ARTIFACT\n')
    async def run():
        state = dict(requests=[], received=asyncio.Event(), disconnected=asyncio.Event(), tool_roundtrip=True)
        peers, built = set(), []
        def connected(reader, writer):
            peers.add(asyncio.create_task(serve_fixture(reader, writer, state=state, provider=provider, delay=None)))
        server = await asyncio.start_server(connected, '127.0.0.1', 0)
        ledger = DurableFixtureLedger(tmp_path / 'ledger.db')
        def build(ref, saved):
            # Exact real canonical admission -> trusted signed bridge. The second
            # queued input intentionally has no card authorization and must DENY,
            # not enter ordinary bootstrap or pause the FIFO.
            if saved['admission_id'] != row['admission_id']:
                raise ValueError('controlled_unapproved_follower')
            policy = NativeSDKPolicy(provider=provider, model='controlled-model', session_id=ref.session_id,
                base_url=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}', accounting='subscription_request_bound')
            launch = DockerLaunch(tmp_path.resolve(), docker_runtime, tmp_path / 'leases', ('allowed.txt',), 'builder')
            bridge, _signer, _resolved = fixture_bridge(authority, saved, launch, policy, ledger)
            built.append(bridge)
            return bridge
        authority._protected_admission_bridge = NativeAdmissionBridgeFactory(authority=authority, build=build)
        ledger.observe = lambda binding: native_protected_readback(authority, binding)
        authority.runner.adapters = {}
        authority.runner._adapter_for_source = lambda source: authority.runner.adapters.get(source.platform)
        ref = restore_local_session(authority, row['target_session_id'])
        live = authority.sessions[ref.session_id]
        events = []
        actual_publish = live.event_stream.publish
        def publish(*args, **kwargs):
            events.append((args, kwargs))
            return actual_publish(*args, **kwargs)
        monkeypatch.setattr(live.event_stream, 'publish', publish)
        follower = admit_session_input(authority.db, epoch=authority.epoch, principal_id=row['principal_id'],
            session_id=ref.session_id, request_id='controlled-follower', payload={'text': 'UNAPPROVED'})
        try:
            await asyncio.wait_for(authority._drain(ref), 25)
            settled = get_session_admission(authority.db, admission_id=row['admission_id'])
            assert (settled['status'], settled['outcome']) == ('terminal', 'completed')
            assert get_session_admission(authority.db, admission_id=follower['admission_id'])['status'] == 'terminal'
            assert not live.pause_notified
            completed = [args[1] for args, kwargs in events if len(args) > 1
                and args[1].get('admission_id') == row['admission_id'] and args[1].get('outcome') == 'completed']
            assert len(completed) == 1 and completed[0]['text'] == 'CONTROLLED_SDK_DONE'
            assert len(built) == 1 and len(state['requests']) == 2 and len(ledger.proofs) == 2
            with authority.db._read_ctx() as conn:
                assert conn.execute("SELECT count(*) FROM state_meta WHERE key=?",
                    ('protected.success.v1.' + row['admission_id'],)).fetchone()[0] == 1
            assert built[0]._runner.turn.proxy.worker.container.absent()
            docker_runtime.record('canonical-drain-' + row['admission_id'], dict(source=built[0].source,
                completed=completed, fifo_paused=live.pause_notified, requests=len(state['requests']),
                namespace_absent=True))
        finally:
            server.close()
            await server.wait_closed()
            await asyncio.wait_for(asyncio.gather(*peers), 2)
    try:
        asyncio.run(run())
    finally:
        authority.db.close()


def test_real_sdk_cumulative_ledger_survives_new_clients(tmp_path, monkeypatch, docker_runtime):
    from gateway.session_protected_native_sdk import NativeSDKPolicy
    from gateway.session_protected_docker import DockerLaunch
    from tests.gateway.test_protected_conversation import native_claim
    authority, _context, row = native_claim(tmp_path, monkeypatch, persisted=True,
        provider='anthropic', model='controlled-model', max_retries=0)
    async def run():
        state = dict(requests=[], received=asyncio.Event(), disconnected=asyncio.Event())
        peers = set()
        def connected(reader, writer):
            peers.add(asyncio.create_task(serve_fixture(reader, writer, state=state, provider='anthropic', delay=None)))
        server = await asyncio.start_server(connected, '127.0.0.1', 0)
        policy = NativeSDKPolicy(provider='anthropic', model='controlled-model', session_id=row['target_session_id'],
            base_url=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}', accounting='subscription_request_bound')
        launch = DockerLaunch(tmp_path.resolve(), docker_runtime, tmp_path / 'leases', ('allowed.txt',), 'builder')
        bridge, _signer, resolved = fixture_bridge(authority, row, launch, policy, DurableFixtureLedger(tmp_path / 'ledger.db'))
        try:
            for index in range(13):
                # Reopen the durable shared ledger; no fresh per-client allowance.
                bridge.ledger = DurableFixtureLedger(tmp_path / 'ledger.db')
                selected = bridge.provider()
                try:
                    if index < 12:
                        await selected.complete([dict(role='user', content='CONTROLLED_CUMULATIVE')], [])
                    else:
                        with pytest.raises(RuntimeError, match='request_failed'):
                            await selected.complete([dict(role='user', content='MUST_NOT_SEND')], [])
                finally:
                    await selected.close()
            assert len(state['requests']) == 12
            assert bridge.budget_state()['requests'] == 12
            # A replaced/reset controller timestamp cannot start another 600s window.
            with sqlite3.connect(tmp_path / 'ledger.db') as conn:
                conn.execute('UPDATE controller SET started=started-601')
            before = len(resolved)
            with pytest.raises((RuntimeError, ValueError)):
                bridge.provider()
            assert len(resolved) == before
        finally:
            server.close()
            await server.wait_closed()
            await asyncio.wait_for(asyncio.gather(*peers), 2)
    try:
        asyncio.run(run())
    finally:
        authority.db.close()
