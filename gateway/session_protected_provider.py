"""Trusted selected async provider; bounded drain, no discovery or credentials."""
import asyncio
import threading

from agent.transports.types import NormalizedResponse

CLOSE_SECONDS = 1.0


class ProviderCloseTimeout(RuntimeError):
    def __init__(self):
        super().__init__('provider_close_timeout')


class ProviderCloseCancelled(RuntimeError):
    def __init__(self):
        super().__init__('provider_close_cancelled')


class ProviderCloseFailed(RuntimeError):
    def __init__(self):
        super().__init__('provider_close_failed')


class SelectedAsyncProvider:
    """Pinned SDK create + ProviderTransport + async SDK close, owner-constructed.

    This cannot forcibly stop a coroutine that suppresses cancellation. Its exact
    tasks remain owned/retryable, but must never hold native process retirement
    hostage. Real SDK construction/cancellation remains a separate acceptance gate.
    """
    def __init__(self, *, model, provider_id, transport, create, aclose):
        if (type(model) is not str or not model or type(provider_id) is not str or not provider_id
                or not callable(create) or not callable(aclose)):
            raise ValueError('explicit selected async provider required')
        self.model, self.provider_id, self.transport = model, provider_id, transport
        self._create, self._aclose = create, aclose
        self._tasks, self._waiters = set(), set()
        self._waiters_lock = threading.Lock()
        self._closed = False
        self._close_task = self._sdk_close_task = None

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

    def submit(self, messages, tools, loop):
        future = asyncio.run_coroutine_threadsafe(self.complete(messages, tools), loop)
        with self._waiters_lock:
            self._waiters.add(future)
            closed = self._closed
        def forget(done):
            with self._waiters_lock:
                self._waiters.discard(done)
        future.add_done_callback(forget)
        if closed:
            future.cancel()
        return future

    def begin_close(self):
        """Revoke and wake IPC waiters without waiting for a cooperative SDK."""
        self._closed = True
        with self._waiters_lock:
            waiters = tuple(self._waiters)
        for future in waiters:
            future.cancel()
        for task in tuple(self._tasks):
            task.cancel()

    async def _close_sdk(self):
        # Include a decorated SDK method's synchronous invocation in the task's
        # normalized fault boundary, as well as exceptions raised while awaiting.
        await self._aclose()

    async def _finish_close(self):
        self.begin_close()
        if self._sdk_close_task is None:
            self._sdk_close_task = asyncio.create_task(self._close_sdk())
            # A late SDK fault stays on the retained task, never becomes an
            # unhandled loop traceback containing a provider/credential body.
            self._sdk_close_task.add_done_callback(
                lambda done: None if done.cancelled() else done.exception())
        pending = set(self._tasks) | {self._sdk_close_task}
        _, unfinished = await asyncio.wait(pending, timeout=CLOSE_SECONDS)
        if unfinished:
            raise ProviderCloseTimeout()
        if self._sdk_close_task.cancelled():
            # SDK self-cancellation is a failed close, not owner cancellation.
            # Keep the exact task for explicit retry and let the sweep continue.
            raise ProviderCloseCancelled()
        error = self._sdk_close_task.exception()
        if error is not None:
            if isinstance(error, (OSError, RuntimeError)):
                raise error
            # Normalize the arbitrary SDK exception universe at this boundary.
            # The original stays on the retained task, never in a wire payload.
            raise ProviderCloseFailed() from None

    async def close(self, *, retry=False):
        if self._close_task is not None and self._close_task.done():
            error = asyncio.CancelledError() if self._close_task.cancelled() else self._close_task.exception()
            drained = not self._tasks and self._sdk_close_task is not None and self._sdk_close_task.done()
            if error is not None and (retry or (isinstance(error, ProviderCloseTimeout) and drained)):
                if retry and self._sdk_close_task is not None and self._sdk_close_task.done():
                    if self._sdk_close_task.cancelled() or self._sdk_close_task.exception() is not None:
                        self._sdk_close_task = None
                self._close_task = None
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._finish_close())
        await asyncio.shield(self._close_task)
