"""Silent retry for a recurring script job's failure that the next run usually heals.

A recurring ``no_agent`` script fails for causes that clear on their own: a GitHub 504 or rate
limit, lock contention, a signal from something outside it (a broad ``pkill``, a supervisor
stopping the host). Paging the operator for one such run is noise, so a failed run of a recurring
``no_agent`` job is withheld until the failures in a row reach the job's alert threshold
(``failure_alert_after``; see ``failure_alert_after``): no failure delivery and no incident-ledger
row, while ``mark_job_run`` still records ``last_failure`` / ``last_status`` / ``failure_streak``
as usual. The run that reaches the threshold alerts, saying how many runs failed in a row and
giving the previous run's cause in one line (naming the signal when a signal killed it).

The older signal rule still applies on top: ``cron.signal_death_grace_runs`` (default 1) sets how
many consecutive signal deaths stay silent; a run is withheld while EITHER rule withholds it.
One-shot jobs and agent jobs keep alerting on the first failure.
"""

from __future__ import annotations

import logging
import re
import signal
from datetime import datetime
from typing import Any, Optional

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

#: ``cron.script_failure_alert_after`` default: a sub-daily script job alerts on its 2nd failure
#: in a row, so a single transient failure that the next run heals never pages.
DEFAULT_SCRIPT_FAILURE_ALERT_AFTER = 2

# A job that runs daily or less often alerts on its first failure by default: waiting for a
# second failure there would hide a real outage for a day or more.
_DAILY_SECONDS = 24 * 60 * 60
# Fire times sampled to find a cron expression's smallest gap ("0 9 * * 1-5" has 24h and 72h gaps).
_CRON_GAP_SAMPLES = 8

#: Cap on the previous run's cause quoted in an alert, so a long stderr stays one line.
_CAUSE_MAX_CHARS = 160

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


def _is_recurring_script_job(job: dict) -> bool:
    """The jobs both withholding rules cover: recurring (``cron``/``interval``) ``no_agent``."""
    return bool(job.get("no_agent")) and (
        (job.get("schedule") or {}).get("kind") in _RECURRING_KINDS)


def _runs_daily_or_less_often(schedule: dict[str, Any]) -> bool:
    """True when every gap between fires is at least 24h: an interval of >= 24h, or a cron
    expression whose smallest gap among its next ``_CRON_GAP_SAMPLES`` fires is >= 24h. Gaps are
    measured on the naive wall clock so a daily job stays daily across a 23h DST day. An
    uncheckable expression (croniter missing, malformed) counts as sub-daily."""
    kind = schedule.get("kind")
    if kind == "interval":
        try:
            return float(schedule.get("minutes") or 0) * 60 >= _DAILY_SECONDS
        except (TypeError, ValueError):
            return False
    if kind != "cron" or not schedule.get("expr"):
        return False
    from cron import jobs

    if not jobs._ensure_croniter():
        return False
    try:
        it = jobs.croniter(schedule["expr"], datetime.now().replace(microsecond=0))
        fires = [it.get_next(datetime) for _ in range(_CRON_GAP_SAMPLES)]
    except (ValueError, KeyError, TypeError):  # croniter's errors subclass ValueError
        return False
    return min((b - a).total_seconds() for a, b in zip(fires, fires[1:])) >= _DAILY_SECONDS


def normalize_failure_alert_after(value: Any) -> Optional[int]:
    """Validate a job's ``failure_alert_after`` (failures in a row before a recurring script job
    alerts); None or '' = unset (the schedule/config default). Bools are refused: ``True == 1``."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not str(value).strip().isdigit() or int(value) < 1:
        raise ValueError(
            f"failure_alert_after must be a whole number of runs, 1 or more, not {value!r}.")
    return int(value)


def failure_alert_after(job: dict) -> int:
    """Consecutive failures a recurring script job needs before one alerts (>= 1).

    The job's own ``failure_alert_after`` wins; unset, a job that runs daily or less often alerts
    on the first failure, and any other job uses ``cron.script_failure_alert_after``."""
    explicit = job.get("failure_alert_after")
    if explicit is not None:
        return max(1, int(explicit))
    if _runs_daily_or_less_often(job.get("schedule") or {}):
        return 1
    from cron.jobs import _cron_config_number

    return max(1, _cron_config_number(
        "script_failure_alert_after", DEFAULT_SCRIPT_FAILURE_ALERT_AFTER, int))


def _one_line(text: Optional[str]) -> str:
    """*text* collapsed to one line of at most ``_CAUSE_MAX_CHARS`` characters."""
    line = re.sub(r"\s+", " ", str(text or "")).strip().rstrip(".") or "unknown error"
    if len(line) > _CAUSE_MAX_CHARS:
        line = line[:_CAUSE_MAX_CHARS - 3].rstrip() + "..."
    return line


def _previous_failure_detail(job: dict) -> Optional[str]:
    """The PREVIOUS run's error when that run failed, else None. ``job`` is the stored record from
    before this run: ``failure_streak`` > 0 means the previous run failed, and ``last_failure``
    (sticky across successes, so only read under a live streak) holds its error."""
    if int(job.get("failure_streak") or 0) <= 0:
        return None
    last_failure = job.get("last_failure")
    return last_failure.get("detail") if isinstance(last_failure, dict) else None


def _previous_run_signal(job: dict) -> Optional[int]:
    """Signal that killed the PREVIOUS run, when that run failed."""
    return script_signal_number(job, _previous_failure_detail(job))


def withhold_signal_death(job: dict, error: Optional[str]) -> bool:
    """True when this failed run is a silent retry: a recurring ``no_agent`` job whose script was
    killed by a signal, inside ``cron.signal_death_grace_runs`` consecutive failures that were all
    signal deaths. Logs the one WARNING line the withheld run leaves."""
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


def withhold_failure_alert(job: dict, error: Optional[str]) -> bool:
    """True when this failed run of a recurring ``no_agent`` job must not alert: the signal rule
    (``withhold_signal_death``) or the streak threshold (``failure_alert_after``) withholds it.
    Logs exactly one WARNING line for a withheld run. The caller skips both the failure delivery
    and the incident upsert, so the run that does alert opens the incident as new."""
    if not _is_recurring_script_job(job):
        return False
    if withhold_signal_death(job, error):
        return True
    failures = int(job.get("failure_streak") or 0) + 1  # +1 = this run, not yet recorded
    threshold = failure_alert_after(job)
    if failures >= threshold:
        return False
    logger.warning(
        "Job '%s' (%s): failed (%s), failure %d in a row of %d before alerting; not alerting, "
        "the next scheduled run is the retry",
        job.get("id"), job.get("name") or job.get("id"), _one_line(error), failures, threshold)
    return True


def previous_failure_note(job: dict) -> str:
    """One line for a failure notice when the previous run also failed, else "": how many runs
    failed in a row and the previous run's cause (the signal, when a signal killed it). Only
    recurring script jobs get the generic line; agent job notices are unchanged."""
    if int(job.get("failure_streak") or 0) <= 0:
        return ""
    signum = _previous_run_signal(job)
    if signum is not None:
        cause = f"its script was killed by signal {signum} ({_signal_name(signum)})"
    elif _is_recurring_script_job(job):
        cause = _one_line(_previous_failure_detail(job))
    else:
        return ""
    failures = int(job.get("failure_streak") or 0) + 1
    return f"\nIt has failed {failures} runs in a row. The previous run also failed: {cause}."
