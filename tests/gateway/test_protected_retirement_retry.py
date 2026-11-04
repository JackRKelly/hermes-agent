"""Real native retirement failure keeps the exact owner reachable for retry."""
import asyncio
from types import SimpleNamespace

import pytest

# Ordinary owner fixtures bootstrap at collection, before the per-test home I/O guard.
# The fresh protected-process tests deliberately do not use this bootstrap.
import gateway.session_protected_scope
from tests.gateway.test_protected_conversation import native_claim


@pytest.mark.platforms('posix')
def test_scope_storage_failure_retains_owner_and_retry_delivery(tmp_path, monkeypatch):
    from gateway import session_worker_reservation as reservation
    from gateway.session_protected_scope import protected_turn_scope, retry_protected_retirements
    from gateway.session_protected_tools import SentinelLaunch
    from agent.transports.types import NormalizedResponse
    from hermes_state_runtime import get_session_admission
    authority, context, admission = native_claim(tmp_path, monkeypatch)
    actual = reservation.lose_admission_worker
    attempts = []
    def fail(*args):
        attempts.append(args)
        raise OSError('controlled retirement I/O')
    monkeypatch.setattr(reservation, 'lose_admission_worker', fail)
    async def exercise():
        captured = []
        with pytest.raises(OSError, match='retirement I/O'):
            async with protected_turn_scope(authority, SimpleNamespace(session_id='owned'), admission,
                    SentinelLaunch(tmp_path.resolve()), principal_id='fixture-owner', context=context,
                    complete=lambda m, t: NormalizedResponse('fixture', None, 'stop')) as turn:
                captured.append(turn)
        assert captured[0].proxy.worker.process.poll() is not None
        pending = authority._protected_retirements
        assert len(pending) == 1
        owner = next(iter(pending.values()))
        assert owner._closing and not owner._closed
        with pytest.raises(Exception, match='protected_capability_denied'):
            owner.client().request('check', {})
        # A second storage failure must retain the same owner, not lose delivery.
        with pytest.raises(OSError, match='retirement I/O'):
            await retry_protected_retirements(authority)
        assert next(iter(pending.values())) is owner
        monkeypatch.setattr(reservation, 'lose_admission_worker', actual)
        await retry_protected_retirements(authority)
        assert not pending
        assert get_session_admission(authority.db, admission_id=admission['admission_id'])['status'] == 'unknown'
        assert authority.db._read_one('SELECT status FROM worker_executions')[0] == 'terminal'
        await retry_protected_retirements(authority)  # idempotent
    try:
        asyncio.run(exercise())
    finally:
        authority.db.close()


@pytest.mark.platforms('posix')
def test_concurrent_eager_and_scope_close_serializes_native_reap(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from gateway.session_protected_scope import protected_turn_scope
    from gateway.session_protected_tools import SentinelLaunch
    from gateway.session_managed_worker import ManagedWorker
    from agent.transports.types import NormalizedResponse
    authority, context, admission = native_claim(tmp_path, monkeypatch)
    actual = ManagedWorker.close
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def slow_close(worker):
        calls.append(worker)
        entered.set()
        assert release.wait(3)
        actual(worker)
    monkeypatch.setattr(ManagedWorker, 'close', slow_close)
    async def exercise():
        async with protected_turn_scope(authority, SimpleNamespace(session_id='owned'), admission,
                SentinelLaunch(tmp_path.resolve()), principal_id='fixture-owner', context=context,
                complete=lambda m, t: NormalizedResponse('fixture', None, 'stop')) as turn:
            worker = turn.proxy.worker
            waiting = threading.Event()
            native_lock = worker._close_lock
            class ObservedLock:
                def __enter__(self):
                    if native_lock.locked():
                        waiting.set()
                    native_lock.acquire()
                def __exit__(self, *args):
                    native_lock.release()
            worker._close_lock = ObservedLock()
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(worker.close)
                assert await asyncio.to_thread(entered.wait, 2)
                second = pool.submit(worker.close)
                assert await asyncio.to_thread(waiting.wait, 2)
                release.set()
                await asyncio.to_thread(first.result)
                await asyncio.to_thread(second.result)
            assert len(calls) == 1
        assert len(calls) == 1
        assert worker.process.poll() is not None
        assert not authority._managed_workers
    try:
        asyncio.run(exercise())
    finally:
        release.set()
        authority.db.close()
