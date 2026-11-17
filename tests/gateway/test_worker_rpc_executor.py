"""Owner-side worker receipts commit on their own pool, not behind the loop's shared default executor.

A ``worker.persist`` queued behind unrelated blocking ``to_thread`` calls ran into the worker's receipt
deadline (Nov 13-14 owner stalls); the owner scope (a ContextVar) must still reach the commit.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextvars
import threading

from tests.waiting import async_wait_until

_scope = contextvars.ContextVar('_scope', default=None)


def test_a_receipt_commits_while_the_default_executor_is_full_and_keeps_the_callers_context():
    from gateway.loop_lag_monitor import executor_state
    from gateway.session_worker import run_worker_rpc
    release = threading.Event()

    def commit(value):
        return value, _scope.get(), threading.current_thread().name

    async def main():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        blocker = asyncio.ensure_future(asyncio.to_thread(release.wait, 30))
        queued = asyncio.ensure_future(asyncio.to_thread(lambda: None))
        try:
            await async_wait_until(lambda: (executor_state(loop) or (0, 0, 0, 0))[3] == 1, timeout=10)
            _scope.set('profile-a')
            return await asyncio.wait_for(run_worker_rpc(commit, 'receipt'), timeout=10)
        finally:
            release.set()
            await asyncio.gather(blocker, queued)
    value, scope, thread = asyncio.run(main())
    assert (value, scope) == ('receipt', 'profile-a')
    assert thread.startswith('worker-rpc')
