"""Owner-owned SOURCE fixture scope; ordinary admission remains disabled."""
import asyncio
import sqlite3
from contextlib import asynccontextmanager

from hermes_state_runtime import RuntimeStoreError

from agent.protected_conversation import ProtectedTurn
from gateway.session_protected_capabilities import NativeLifecycle, ScopedInference
from gateway.session_protected_tools import ToolProxy


async def _drain_owned(task, cancellations):
    """Finish acquisition/teardown despite caller cancellation, then propagate it."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellations.append(True)
    return task.result()


def _prepare(worker, launch, authority, row, principal_id, context, complete):
    from hermes_state_runtime import RuntimeStoreError
    if worker.stop.is_set():
        raise RuntimeStoreError('managed_worker_stopped')
    proxy = ToolProxy(worker, launch)
    proxy.bind()
    if worker.stop.is_set():
        raise RuntimeStoreError('managed_worker_stopped')
    owner = NativeLifecycle.reserve(authority, admission_id=row['admission_id'],
        principal_id=principal_id, process=worker.process, context=context)
    try:
        inference = ScopedInference(complete, owner.binding)
        turn = ProtectedTurn(inference.client(), owner.client(), proxy)
        # Owner-only handles: the fresh child receives bounded channel configs,
        # never these objects or the canonical database/adoption authority.
        turn._native_owner = owner
        turn._inference_endpoint = inference
        return owner, turn
    except BaseException:
        owner.close()
        raise


async def retry_protected_retirements(authority):
    """Retry exact retained native retirements; errors stay visible and reachable.

    This is owner-only, never an inference/tool capability. No new reservation or
    delivery ID is minted. Call before closing the authority DB after a failed scope.
    """
    from gateway.session_protected_capabilities import _retirement_registry
    lock, pending = _retirement_registry(authority)
    with lock:
        owners = tuple(pending.values())
    cancellations, failures = [], []
    for owner in owners:
        try:
            await _drain_owned(asyncio.create_task(asyncio.to_thread(owner.close)), cancellations)
        except (OSError, RuntimeStoreError, sqlite3.Error) as exc:
            failures.append(exc)
    if cancellations:
        raise asyncio.CancelledError
    if failures:
        raise failures[0]


@asynccontextmanager
async def protected_turn_scope(authority, ref, row, launch, *, principal_id, context, complete):
    """Acquire native source fixture state; always attempt retirement and reap.

    Legacy synchronous callbacks are controlled fixtures, not cancellable provider
    calls. The fresh protected_process_scope owns the selected async provider and
    actual IPC. Blocking native bind/store/close work remains off-loop here.
    """
    from gateway.session_managed_worker import _spawn_registered_worker, _mark_spawning, _stopped_before_spawn
    from hermes_state_runtime import RuntimeStoreError
    if getattr(authority, 'pending_stops', {}).get(ref.session_id) == row.get('generation'):
        _mark_spawning(authority, ref, row)
        _stopped_before_spawn(authority, ref, row)
        raise RuntimeStoreError('managed_worker_stopped')
    worker, _ = await _spawn_registered_worker(authority, ref, row, sentinel=launch)
    # Use the canonical Stop latch/delivery interface, including Stops admitted
    # while acquire_process was off-loop. Never clear an earlier queued Stop.
    try:
        authority.adopt_agent(ref.session_id, row['generation'], worker)
    except BaseException:
        await asyncio.to_thread(worker.close)
        raise
    pending = asyncio.create_task(asyncio.to_thread(_prepare, worker, launch, authority, row, principal_id, context, complete))
    try:
        _, turn = await asyncio.shield(pending)
        yield turn
    finally:
        cancellations = []
        try:
            owner, _ = await _drain_owned(pending, cancellations)
            await _drain_owned(asyncio.create_task(asyncio.to_thread(owner.close)), cancellations)
        finally:
            try:
                await _drain_owned(asyncio.create_task(asyncio.to_thread(worker.close)), cancellations)
            finally:
                if authority.adopted.get(ref.session_id) == (row['generation'], worker):
                    authority.adopted.pop(ref.session_id)
                if cancellations:
                    raise asyncio.CancelledError
