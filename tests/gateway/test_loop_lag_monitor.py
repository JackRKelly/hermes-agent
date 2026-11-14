"""The gateway names what stalls its loop and what fills its default executor, while it happens.

Live incident (Nov 13-14): owner-wide stalls failed every worker's persistence receipt at once and left
no line saying what the loop or its executor was doing.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time

from tests.waiting import async_wait_until, wait_until


def _blocking_section_on_the_loop(release):
    # Named so the captured loop stack is recognisable.
    release.wait(30)


def _messages(caplog, needle):
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


def test_a_blocked_loop_is_reported_with_its_stack_and_its_lateness(caplog):
    from gateway.loop_lag_monitor import start_loop_lag_monitor
    caplog.set_level(logging.WARNING, logger='gateway.loop_lag_monitor')
    release = threading.Event()

    async def main():
        loop = asyncio.get_running_loop()
        monitor = start_loop_lag_monitor(loop, interval=0.1, threshold=0.5)
        try:
            beat = monitor.beat
            await async_wait_until(lambda: monitor.beat != beat, timeout=10, what='a first beat')
            # Released once the off-loop half has reported the stall (10 s backstop).
            threading.Thread(target=lambda: (wait_until(lambda: _messages(caplog, 'blocked for'), timeout=10),
                                              release.set()), daemon=True).start()
            _blocking_section_on_the_loop(release)
            await async_wait_until(lambda: _messages(caplog, 'event loop stalled'), timeout=10,
                                   what='the first beat after the stall')
        finally:
            monitor.stop()
    asyncio.run(main())
    blocked = _messages(caplog, 'blocked for')
    assert len(blocked) == 1, blocked  # once per stall, not once per check
    assert '_blocking_section_on_the_loop' in blocked[0]


def test_a_closed_loop_is_not_reported_as_stalled_and_ends_the_monitor(caplog):
    from gateway.loop_lag_monitor import start_loop_lag_monitor
    caplog.set_level(logging.WARNING, logger='gateway.loop_lag_monitor')

    async def main():
        return start_loop_lag_monitor(asyncio.get_running_loop(), interval=0.1, threshold=0.5)
    monitor = asyncio.run(main())  # never stopped: the loop just closes under it
    monitor.check(time.monotonic() + 60)
    assert not _messages(caplog, 'blocked for')
    wait_until(lambda: not monitor._thread.is_alive(), timeout=10, what='the monitor thread to exit')


def test_a_saturated_default_executor_is_reported_with_what_its_threads_run(caplog):
    from gateway.loop_lag_monitor import LoopLagMonitor, executor_state
    caplog.set_level(logging.WARNING, logger='gateway.loop_lag_monitor')
    release = threading.Event()

    def _held_executor_call():
        release.wait(30)

    async def main():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
        calls = [asyncio.ensure_future(asyncio.to_thread(_held_executor_call)) for _ in range(5)]
        try:
            await async_wait_until(lambda: (executor_state(loop) or (0, 0, 0, 0))[3] == 3, timeout=10,
                                   what='two busy threads and three queued calls')
            monitor = LoopLagMonitor(loop, threshold=1.0)
            now = time.monotonic()
            for offset in (0.0, 0.5, 1.5, 2.5):
                monitor.beat = now + offset  # the loop itself is live here: only the pool is full
                monitor.check(now + offset)
                if offset == 0.5:
                    assert not _messages(caplog, 'saturated')  # not yet past the threshold
        finally:
            release.set()
            await asyncio.gather(*calls)
    asyncio.run(main())
    saturated = _messages(caplog, 'saturated')
    assert len(saturated) == 1, saturated  # once per episode
    assert 'busy=2 threads=2/2 queued=3' in saturated[0]
    assert '2x' in saturated[0] and '_held_executor_call' in saturated[0]
