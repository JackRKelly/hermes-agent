"""Silent retry for a recurring script job whose first failure in a row was a signal death.

A ``no_agent`` script that dies because something outside it sent a signal (a broad ``pkill``,
a supervisor stopping the host) did not find a problem with the job: the next scheduled run
usually succeeds. Paging the operator for that one run is noise, so the FIRST such failure in a
row of a recurring job is withheld: no failure delivery and no incident-ledger row, while
``mark_job_run`` still records ``last_failure`` / ``last_status`` / ``failure_streak`` as usual.
The next failed run (any cause) alerts normally, with one line naming the signal that killed the
previous run. ``cron.signal_death_grace_runs`` (default 1) sets how many consecutive signal deaths
stay silent; 0 alerts on the first, as before. One-shot jobs and every other failure kind keep
alerting on the first failure.
"""

from __future__ import annotations

import logging
import re
import signal
from typing import Optional

logger = logging.getLogger("cron.scheduler")

#: The script runner's failure text for a non-zero exit (``scheduler_script._run_job_script``).
#: The runner returns ``(ok, text)`` with no structured return code, so the code is read back from
#: this one prefix; both sides use this constant so the two cannot drift apart.
SCRIPT_EXIT_PREFIX = "Script exited with code "
_SCRIPT_EXIT_RE = re.compile(r"\A" + re.escape(SCRIPT_EXIT_PREFIX) + r"(-?\d+)\b")

# A shell that runs a child killed by signal N exits 128+N, so a ``.sh`` wrapper reports the
# child's SIGTERM as 143 rather than -15. Only the signals used to stop a process count; 128+N
# for a crash signal (SIGSEGV 139) is the script's own fault and alerts on the first run.
# POSIX numbers, spelled out because ``signal.SIGHUP``/``SIGKILL`` do not exist on Windows.
_SHELL_WRAPPED_STOP_SIGNALS = {1: "SIGHUP", 2: "SIGINT", 9: "SIGKILL", 15: "SIGTERM"}

DEFAULT_SIGNAL_DEATH_GRACE_RUNS = 1

_RECURRING_KINDS = frozenset({"cron", "interval"})


def script_signal_number(job: dict, error: Optional[str]) -> Optional[int]:
    """The signal that killed this ``no_agent`` job's script, read from the runner's failure
    text, or None when the failure was anything else (non-zero exit, timeout, cancel, agent job).

    A negative return code is a direct signal death (any signal); 128+N counts only for the stop
    signals in ``_SHELL_WRAPPED_STOP_SIGNALS``, the exit status a shell wrapper reports."""
    if not job.get("no_agent"):
        return None
    match = _SCRIPT_EXIT_RE.match(str(error or ""))
    if match is None:
        return None
    code = int(match.group(1))
    if code < 0:
        return -code
    signum = code - 128
    return signum if signum in _SHELL_WRAPPED_STOP_SIGNALS else None


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return _SHELL_WRAPPED_STOP_SIGNALS.get(signum, f"signal {signum}")


def _grace_runs() -> int:
    """``cron.signal_death_grace_runs``: consecutive signal deaths withheld before one alerts."""
    from cron.jobs import _cron_config_number

    return max(0, _cron_config_number(
        "signal_death_grace_runs", DEFAULT_SIGNAL_DEATH_GRACE_RUNS, int))


def _previous_run_signal(job: dict) -> Optional[int]:
    """Signal that killed the PREVIOUS run, when that run failed. ``job`` is the stored record
    from before this run: ``failure_streak`` > 0 means the previous run failed, and
    ``last_failure`` (sticky across successes, so only read under a live streak) holds its error."""
    if int(job.get("failure_streak") or 0) <= 0:
        return None
    last_failure = job.get("last_failure")
    detail = last_failure.get("detail") if isinstance(last_failure, dict) else None
    return script_signal_number(job, detail)


def withhold_signal_death(job: dict, error: Optional[str]) -> bool:
    """True when this failed run is a silent retry: a recurring ``no_agent`` job whose script was
    killed by a signal, inside ``cron.signal_death_grace_runs`` consecutive failures that were all
    signal deaths. Logs the one WARNING line the withheld run leaves. The caller skips both the
    failure delivery and the incident upsert, so the next failure still alerts as a new incident."""
    if (job.get("schedule") or {}).get("kind") not in _RECURRING_KINDS:
        return False
    signum = script_signal_number(job, error)
    if signum is None:
        return False
    streak = int(job.get("failure_streak") or 0)
    if streak >= _grace_runs():
        return False
    # A streak that began with a real failure already alerted; a later signal death in it is not
    # a fresh "first failure", so only an all-signal-death streak stays silent.
    if streak and _previous_run_signal(job) is None:
        return False
    logger.warning(
        "Job '%s' (%s): script killed by signal %d (%s), failure %d in a row; not alerting, "
        "the next scheduled run is the retry",
        job.get("id"), job.get("name") or job.get("id"), signum, _signal_name(signum),
        streak + 1)
    return True


def previous_signal_death_note(job: dict) -> str:
    """One line for a failure notice when the previous run was killed by a signal, else ""."""
    signum = _previous_run_signal(job)
    if signum is None:
        return ""
    return (
        f"\nThe previous run also failed: its script was killed by signal {signum} "
        f"({_signal_name(signum)}).")
