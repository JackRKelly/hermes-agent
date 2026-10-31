"""Owner-owned SOURCE fixture scope; ordinary admission remains disabled."""
import asyncio
from contextlib import asynccontextmanager

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
    proxy = ToolProxy(worker, launch)
    proxy.bind()
    owner = NativeLifecycle.reserve(authority, admission_id=row['admission_id'],
        principal_id=principal_id, process=worker.process, context=context)
    try:
        turn = ProtectedTurn(ScopedInference(complete, owner.binding).client(), owner.client(), proxy)
        return owner, turn
    except BaseException:
        owner.close()
        raise


@asynccontextmanager
async def protected_turn_scope(authority, ref, row, launch, *, principal_id, context, complete):
    """Acquire through native spawn; always attempt retirement and ensure reap.

    No custom admission, scheduler or owner RPC. Blocking channel/store work runs
    off-loop. Invoke the yielded synchronous public conversation with to_thread;
    real inference transport deadlines remain an explicit production gap.
    """
    from gateway.session_managed_worker import _spawn_registered_worker
    worker, _ = await _spawn_registered_worker(authority, ref, row, sentinel=launch)
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
            await _drain_owned(asyncio.create_task(asyncio.to_thread(worker.close)), cancellations)
            if cancellations:
                raise asyncio.CancelledError
