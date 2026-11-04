"""Trusted owner-only closed pilot profile; never parsed from worker/config JSON."""
from dataclasses import dataclass
import math
import time


@dataclass(frozen=True)
class PilotTiming:
    request_seconds: float = 10.0
    turn_seconds: float = 25.0
    tool_seconds: float = 5.0
    shell_seconds: float = 2.0
    namespace_seconds: float = 30.0
    close_seconds: float = 1.0

    def validate(self):
        # One reviewed coherent profile, not a general timeout tuning interface.
        expected = (10, 25, 5, 2, 30, 1)
        values = (self.request_seconds, self.turn_seconds, self.tool_seconds,
                  self.shell_seconds, self.namespace_seconds, self.close_seconds)
        if any(type(v) not in (int, float) or not math.isfinite(v) or v != e
               for v, e in zip(values, expected)):
            raise RuntimeError('protected_pilot_timing_denied')


@dataclass(frozen=True, init=False)
class TurnDeadline:
    """Single owner-created absolute deadline, never renewed by a call/channel."""
    timing: PilotTiming
    deadline: float
    namespace_expires_at: float

    def __init__(self, timing, controller_deadline):
        if type(timing) is not PilotTiming:
            raise RuntimeError('protected_pilot_timing_denied')
        timing.validate()
        if not math.isfinite(controller_deadline):
            raise RuntimeError('protected_pilot_timing_denied')
        object.__setattr__(self, 'timing', timing)
        object.__setattr__(self, 'deadline', min(time.monotonic() + timing.turn_seconds, controller_deadline))
        # PID1 has a different monotonic origin in Docker Desktop. Transfer only
        # an absolute wall expiry, converted ONCE to its boot clock on entry.
        # The namespace's cleanup margin never extends the original controller.
        object.__setattr__(self, 'namespace_expires_at', time.time() + min(self.deadline + 5, controller_deadline) - time.monotonic())
        self.remaining()

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise RuntimeError('protected_pilot_deadline_expired')
        return value

    def binding(self):
        return dict(deadline=self.deadline, turn_seconds=self.timing.turn_seconds)
