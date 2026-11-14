"""Name what stalls the gateway loop, while it is stalled.

Managed workers wait on the owner's loop for every persistence receipt and their ``finish``; an owner
stall long enough to miss those deadlines left only ``ws response send failed`` lines behind, with
nothing naming the blocker. ``shutdown_watchdog``'s liveness probe acts on stalls of minutes and says
nothing below that. This monitor reports at seconds and costs one ``call_later`` per second on the loop
plus one sleeping thread; it only formats stacks while something is late.

- The loop stamps a beat every ``interval``; a beat later than ``threshold`` logs how late it was.
- A thread off the loop sees a beat go stale and logs, once per stall while it is still going, the loop
  thread's current stack and the default executor's state.
- The same thread logs, once per episode, a default executor with queued work and no idle thread,
  with what its busy threads are running (that queue delays every ``asyncio.to_thread`` caller).
"""
from __future__ import annotations

import collections
import logging
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 1.0
DEFAULT_THRESHOLD_S = 2.0
_STACK_FRAMES = 12
# Frames of the pool's own machinery (an idle worker has nothing else on its stack).
_EXECUTOR_MODULES = ('/concurrent/futures/thread.py', '\\concurrent\\futures\\thread.py', '/threading.py', '\\threading.py')


def executor_state(loop):
    """``(busy, threads, max_workers, queued)`` of the loop's default executor, or None before its
    first use. Reads ThreadPoolExecutor internals: a diagnostic, never a decision."""
    executor = getattr(loop, '_default_executor', None)
    if executor is None:
        return None
    try:
        threads = len(executor._threads)
        idle = executor._idle_semaphore._value
        return max(0, threads - idle), threads, executor._max_workers, executor._work_queue.qsize()
    except AttributeError:
        return None


def _stack(frame, limit=_STACK_FRAMES):
    return ''.join(traceback.format_stack(frame, limit=limit)).rstrip()


def _where(frame):
    return f'{frame.f_code.co_filename}:{frame.f_lineno} {frame.f_code.co_name}'


def _busy_threads(loop):
    """``Counter`` of ``innermost <- submitted function`` for each default-executor thread doing work."""
    threads = getattr(getattr(loop, '_default_executor', None), '_threads', None) or ()
    frames = sys._current_frames()
    busy = collections.Counter()
    for thread in list(threads):
        frame, work = frames.get(thread.ident), []
        while frame is not None:
            if not frame.f_code.co_filename.endswith(_EXECUTOR_MODULES):
                work.append(frame)
            frame = frame.f_back
        if work:  # an idle worker has only the pool's own frames
            busy[_where(work[0]) if len(work) == 1 else f'{_where(work[0])} <- {_where(work[-1])}'] += 1
    return busy


def _describe_executor(loop):
    state = executor_state(loop)
    if state is None:
        return 'default executor: unused'
    busy, threads, workers, queued = state
    return f'default executor: busy={busy} threads={threads}/{workers} queued={queued}'


class LoopLagMonitor:
    """Start on the loop thread (``start`` records it); ``stop`` ends both halves."""

    def __init__(self, loop, *, interval=DEFAULT_INTERVAL_S, threshold=DEFAULT_THRESHOLD_S):
        self.loop, self.interval, self.threshold = loop, interval, threshold
        self.loop_thread = None
        self.beat = time.monotonic()
        self._reported_beat = None
        self._saturated_since = None
        self._saturation_reported = False
        self._stopped = threading.Event()
        self._timer = None
        self._thread = threading.Thread(  # health: allow HX012 -- process-wide probe; reads only the loop and thread frames
            target=self._watch, name='gateway-loop-lag-monitor', daemon=True)

    def start(self):
        self.loop_thread = threading.get_ident()
        self.beat = time.monotonic()
        self._timer = self.loop.call_later(self.interval, self._tick, self.beat + self.interval)
        self._thread.start()
        return self

    def stop(self):
        self._stopped.set()
        if self._timer is not None:
            self._timer.cancel()

    def _tick(self, expected):
        now = time.monotonic()
        self.beat = now
        if now - expected > self.threshold:
            logger.warning('gateway event loop stalled %.1fs (%s)', now - expected, _describe_executor(self.loop))
        if not self._stopped.is_set():
            self._timer = self.loop.call_later(self.interval, self._tick, now + self.interval)

    def _watch(self):
        while not self._stopped.wait(self.interval):
            self.check(time.monotonic())

    def check(self, now):
        """One observation off the loop; public so a test can drive it deterministically."""
        if self.loop.is_closed():
            self._stopped.set()
            return
        if not self.loop.is_running():  # a loop that is not running is not stalled
            return
        beat = self.beat
        late = now - beat - self.interval
        if late > self.threshold and self._reported_beat != beat:
            self._reported_beat = beat
            frame = sys._current_frames().get(self.loop_thread) if self.loop_thread is not None else None
            logger.warning('gateway event loop blocked for %.1fs so far (%s); loop thread at:\n%s',
                           late, _describe_executor(self.loop),
                           _stack(frame) if frame is not None else '(loop thread not found)')
        self._check_executor(now)

    def _check_executor(self, now):
        state = executor_state(self.loop)
        saturated = state is not None and state[3] > 0 and state[0] >= state[2]
        if not saturated:
            self._saturated_since, self._saturation_reported = None, False
            return
        if self._saturated_since is None:
            self._saturated_since = now
        if self._saturation_reported or now - self._saturated_since < self.threshold:
            return
        self._saturation_reported = True
        busy, threads, workers, queued = state
        running = '; '.join(f'{count}x {where}' for where, count in _busy_threads(self.loop).most_common(8))
        logger.warning('gateway default executor saturated for %.1fs: busy=%d threads=%d/%d queued=%d; running: %s',
                       now - self._saturated_since, busy, threads, workers, queued, running or '(unknown)')


def start_loop_lag_monitor(loop, **kwargs):
    """Arm the monitor from a coroutine on ``loop``; returns it (``stop()`` disarms)."""
    return LoopLagMonitor(loop, **kwargs).start()
