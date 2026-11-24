"""A recurring script job's first signal death is a silent retry; the next failure alerts.

Incident (Nov 24 2026): a broad ``pkill`` SIGTERMed the script of a 2-minute ``no_agent`` job.
The scheduler recorded ``Script exited with code -15`` and paged the operator; the next run two
minutes later succeeded. These tests drive real scripts through ``run_one_job`` against a real
job store and incident ledger in a temp ``HERMES_HOME``; only the outbound send is captured.
"""

from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.platforms("posix")  # real bash scripts killed by real signals

SIGTERM_SELF = "#!/usr/bin/env bash\nkill -TERM $$\n"
# A shell wrapper whose child dies by SIGTERM: bash reports the child as 128+15 and exits with it.
SIGTERM_WRAPPED = "#!/usr/bin/env bash\nbash -c 'kill -TERM $$'\nexit $?\n"
PLAIN_FAILURE = "#!/usr/bin/env bash\necho broken >&2\nexit 3\n"
SILENT_SUCCESS = "#!/usr/bin/env bash\nexit 0\n"


@pytest.fixture
def cron_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "scripts").mkdir(parents=True)
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # cron.jobs resolves its store paths at import; reload so they land in this home.
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    return home


@pytest.fixture
def sent(cron_home, monkeypatch):
    """Every failure/success notice that would leave the process, as ``(job_id, text)``."""
    import cron.scheduler as sched

    out: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sched, "_deliver_result",
        lambda job, content, **_kw: out.append((job["id"], content)) or None)
    return out


def _grace(monkeypatch, runs: int) -> None:
    from hermes_cli import config

    real = config.load_config
    monkeypatch.setattr(
        config, "load_config",
        lambda *a, **kw: {**real(*a, **kw), "cron": {"signal_death_grace_runs": runs}})


def _job(home, schedule: str = "every 5m"):
    from cron.jobs import create_job

    (home / "scripts" / "probe.sh").write_text(SILENT_SUCCESS, encoding="utf-8")
    return create_job(
        prompt=None, schedule=schedule, script="probe.sh", no_agent=True,
        deliver="telegram:123")


def _run(home, job_id: str, script: str) -> dict:
    """One scheduled run with *script* as the job's body; returns the stored record after it."""
    from cron.jobs import get_job
    from cron.scheduler import run_one_job

    (home / "scripts" / "probe.sh").write_text(script, encoding="utf-8")
    assert run_one_job(get_job(job_id)) is True
    return get_job(job_id)


def _incident_count(job_id: str) -> int:
    from cron.incidents import list_incidents

    return len([row for row in list_incidents() if row["job_id"] == job_id])


def test_first_sigterm_death_is_silent_but_recorded(cron_home, sent, caplog):
    job = _job(cron_home)

    with caplog.at_level("WARNING", logger="cron.scheduler"):
        after = _run(cron_home, job["id"], SIGTERM_SELF)

    assert sent == []
    assert _incident_count(job["id"]) == 0
    assert after["failure_streak"] == 1
    assert after["last_status"] == "error"
    assert after["last_failure"]["detail"].startswith("Script exited with code -15")
    warnings = [r.getMessage() for r in caplog.records if "killed by signal 15" in r.getMessage()]
    assert len(warnings) == 1 and job["id"] in warnings[0] and "SIGTERM" in warnings[0]


def test_second_consecutive_failure_alerts_and_names_the_signal(cron_home, sent):
    job = _job(cron_home)
    _run(cron_home, job["id"], SIGTERM_SELF)

    after = _run(cron_home, job["id"], PLAIN_FAILURE)

    assert len(sent) == 1
    assert "previous run also failed: its script was killed by signal 15 (SIGTERM)" in sent[0][1]
    assert _incident_count(job["id"]) == 1
    assert after["failure_streak"] == 2


def test_two_sigterm_deaths_in_a_row_alert_on_the_second(cron_home, sent):
    job = _job(cron_home)
    _run(cron_home, job["id"], SIGTERM_SELF)
    _run(cron_home, job["id"], SIGTERM_SELF)

    assert len(sent) == 1
    assert "killed by signal 15 (SIGTERM)" in sent[0][1]


def test_sigterm_success_sigterm_stays_silent_both_times(cron_home, sent):
    job = _job(cron_home)
    _run(cron_home, job["id"], SIGTERM_SELF)
    assert _run(cron_home, job["id"], SILENT_SUCCESS)["failure_streak"] == 0
    after = _run(cron_home, job["id"], SIGTERM_SELF)

    assert sent == []
    assert _incident_count(job["id"]) == 0
    assert after["failure_streak"] == 1


def test_non_signal_first_failure_still_alerts(cron_home, sent):
    job = _job(cron_home)
    _run(cron_home, job["id"], PLAIN_FAILURE)

    assert len(sent) == 1
    assert "previous run" not in sent[0][1]
    assert _incident_count(job["id"]) == 1


def test_grace_zero_alerts_on_the_first_signal_death(cron_home, sent, monkeypatch):
    _grace(monkeypatch, 0)
    job = _job(cron_home)
    _run(cron_home, job["id"], SIGTERM_SELF)

    assert len(sent) == 1
    assert _incident_count(job["id"]) == 1


def test_one_shot_job_alerts_on_the_first_signal_death(cron_home, sent):
    job = _job(cron_home, schedule="30m")
    assert job["schedule"]["kind"] == "once"
    _run(cron_home, job["id"], SIGTERM_SELF)

    assert len(sent) == 1


def test_shell_wrapper_exit_143_counts_as_a_signal_death(cron_home, sent):
    job = _job(cron_home)
    after = _run(cron_home, job["id"], SIGTERM_WRAPPED)

    assert after["last_failure"]["detail"].startswith("Script exited with code 143")
    assert sent == []
    assert _incident_count(job["id"]) == 0


def test_crash_path_uses_the_same_silent_retry_gate(cron_home, sent):
    """A failure that raises out of run_job leaves through _deliver_crash_failure: the gate holds
    there too, and a real crash text still alerts."""
    import cron.scheduler as sched

    job = {"id": "crash", "name": "c", "no_agent": True, "deliver": "telegram:1",
           "schedule": {"kind": "interval", "minutes": 2}, "failure_streak": 0}
    assert sched._deliver_crash_failure(
        job, "Script exited with code -9", adapters=None, loop=None) == (None, "suppressed")
    assert sent == []

    sched._deliver_crash_failure(job, "RuntimeError: boom", adapters=None, loop=None)
    assert len(sent) == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("Script exited with code -15\nstderr:\nx", 15),
        ("Script exited with code -9", 9),
        ("Script exited with code 143", 15),
        ("Script exited with code 137", 9),
        ("Script exited with code 129", 1),
        ("Script exited with code 130", 2),
        ("Script exited with code 139", None),  # SIGSEGV via a shell: a crash, alerts
        ("Script exited with code 1", None),
        ("Script timed out after 60s: /x.sh", None),
        ("stdout:\nScript exited with code -15", None),  # only the runner's leading line counts
    ],
)
def test_script_signal_number(error, expected):
    from cron.scheduler_signal_retry import script_signal_number

    assert script_signal_number({"no_agent": True}, error) == expected


def test_agent_jobs_are_never_signal_deaths():
    from cron.scheduler_signal_retry import script_signal_number

    assert script_signal_number({"no_agent": False}, "Script exited with code -15") is None
