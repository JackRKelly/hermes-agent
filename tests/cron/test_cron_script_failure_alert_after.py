"""A recurring script job alerts only after ``failure_alert_after`` failures in a row.

Incident class (Nov 2026): single failed runs of recurring ``no_agent`` script jobs (a GitHub 504
or rate limit, a SIGTERM, lock contention) paged the operator although the next run succeeded.
These tests drive real scripts through ``run_one_job`` against a real job store, incident ledger
and ``config.yaml`` in a temp ``HERMES_HOME``; only the outbound send is captured.
"""

from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.platforms("posix")  # real bash scripts

PLAIN_FAILURE = "#!/usr/bin/env bash\necho 'HTTP 504 from api.github.com' >&2\nexit 3\n"
LOCK_FAILURE = "#!/usr/bin/env bash\necho 'lock held by another run' >&2\nexit 4\n"
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


def _config(home, **cron_keys) -> None:
    """Write ``cron.<key>: <value>`` into the home's real config.yaml (read by the loader)."""
    body = "".join(f"  {key}: {value}\n" for key, value in cron_keys.items())
    (home / "config.yaml").write_text(f"cron:\n{body}", encoding="utf-8")


def _job(home, schedule: str = "every 5m", **fields):
    from cron.jobs import create_job

    (home / "scripts" / "probe.sh").write_text(SILENT_SUCCESS, encoding="utf-8")
    return create_job(
        prompt=None, schedule=schedule, script="probe.sh", no_agent=True,
        deliver="telegram:123", **fields)


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


def test_first_failure_is_withheld_second_alerts_with_previous_cause(cron_home, sent, caplog):
    job = _job(cron_home)

    with caplog.at_level("WARNING", logger="cron.scheduler"):
        after = _run(cron_home, job["id"], PLAIN_FAILURE)

    assert sent == []
    assert _incident_count(job["id"]) == 0
    assert after["failure_streak"] == 1
    assert after["last_status"] == "error"
    assert after["last_failure"]["detail"].startswith("Script exited with code 3")
    warnings = [r.getMessage() for r in caplog.records if "failure 1 in a row of 2" in r.getMessage()]
    assert len(warnings) == 1 and job["id"] in warnings[0] and "code 3" in warnings[0]

    after = _run(cron_home, job["id"], LOCK_FAILURE)

    assert len(sent) == 1
    text = sent[0][1]
    assert "It has failed 2 runs in a row." in text
    assert "previous run also failed: Script exited with code 3 stderr: HTTP 504" in text
    assert _incident_count(job["id"]) == 1
    assert after["failure_streak"] == 2


def test_fail_ok_fail_never_pages(cron_home, sent):
    job = _job(cron_home)
    _run(cron_home, job["id"], PLAIN_FAILURE)
    assert _run(cron_home, job["id"], SILENT_SUCCESS)["failure_streak"] == 0
    after = _run(cron_home, job["id"], PLAIN_FAILURE)

    assert sent == []
    assert _incident_count(job["id"]) == 0
    assert after["failure_streak"] == 1


def test_failure_alert_after_one_alerts_on_the_first(cron_home, sent):
    job = _job(cron_home, failure_alert_after=1)
    _run(cron_home, job["id"], PLAIN_FAILURE)

    assert len(sent) == 1
    assert "previous run" not in sent[0][1]
    assert _incident_count(job["id"]) == 1


def test_failure_alert_after_three_withholds_two(cron_home, sent):
    job = _job(cron_home, failure_alert_after=3)
    _run(cron_home, job["id"], PLAIN_FAILURE)
    _run(cron_home, job["id"], PLAIN_FAILURE)
    assert sent == []
    assert _incident_count(job["id"]) == 0

    _run(cron_home, job["id"], PLAIN_FAILURE)
    assert len(sent) == 1
    assert "It has failed 3 runs in a row." in sent[0][1]


@pytest.mark.parametrize("schedule", ["0 9 * * *", "1d", "every 24h"])
def test_daily_or_slower_job_alerts_on_the_first_by_default(cron_home, sent, schedule):
    job = _job(cron_home, schedule=schedule)
    assert job["schedule"]["kind"] in {"cron", "interval"}
    _run(cron_home, job["id"], PLAIN_FAILURE)

    assert len(sent) == 1
    assert _incident_count(job["id"]) == 1


def test_explicit_value_wins_over_the_daily_default(cron_home, sent):
    job = _job(cron_home, schedule="0 9 * * *", failure_alert_after=2)
    _run(cron_home, job["id"], PLAIN_FAILURE)

    assert sent == []


def test_global_config_sets_the_sub_daily_default(cron_home, sent):
    _config(cron_home, script_failure_alert_after=3)
    job = _job(cron_home)
    _run(cron_home, job["id"], PLAIN_FAILURE)
    _run(cron_home, job["id"], PLAIN_FAILURE)
    assert sent == []

    _run(cron_home, job["id"], PLAIN_FAILURE)
    assert len(sent) == 1


def test_global_config_one_restores_alert_on_first(cron_home, sent):
    _config(cron_home, script_failure_alert_after=1)
    job = _job(cron_home)
    _run(cron_home, job["id"], PLAIN_FAILURE)

    assert len(sent) == 1


def test_one_shot_script_job_alerts_on_the_first(cron_home, sent):
    job = _job(cron_home, schedule="2030-01-01T09:00:00Z")
    assert job["schedule"]["kind"] == "once"
    _run(cron_home, job["id"], PLAIN_FAILURE)

    assert len(sent) == 1


def test_agent_job_alerts_on_the_first(cron_home, sent):
    """An agent job's failure leaves through the same gate; the threshold never applies to it."""
    import cron.scheduler as sched

    job = {"id": "agent", "name": "a", "no_agent": False, "deliver": "telegram:1",
           "schedule": {"kind": "interval", "minutes": 5}, "failure_streak": 0}
    sched._deliver_crash_failure(job, "RuntimeError: boom", adapters=None, loop=None)

    assert len(sent) == 1
    assert _incident_count("agent") == 1


def test_crash_path_uses_the_same_threshold(cron_home, sent):
    import cron.scheduler as sched

    job = {"id": "crash", "name": "c", "no_agent": True, "deliver": "telegram:1",
           "schedule": {"kind": "interval", "minutes": 5}, "failure_streak": 0}
    assert sched._deliver_crash_failure(
        job, "RuntimeError: boom", adapters=None, loop=None) == (None, "suppressed")
    assert sent == []
    assert _incident_count("crash") == 0

    job.update(failure_streak=1, last_failure={"at": "x", "detail": "RuntimeError: boom"})
    sched._deliver_crash_failure(job, "RuntimeError: boom", adapters=None, loop=None)
    assert len(sent) == 1
    assert "It has failed 2 runs in a row." in sent[0][1]


@pytest.mark.parametrize("value", [0, -1, "two", True, 1.5])
def test_create_refuses_an_invalid_failure_alert_after(cron_home, value):
    with pytest.raises(ValueError, match="failure_alert_after"):
        _job(cron_home, failure_alert_after=value)


def test_failure_alert_after_is_stored_only_when_set_and_updatable(cron_home):
    from cron.jobs import update_job

    job = _job(cron_home)
    assert "failure_alert_after" not in job
    assert update_job(job["id"], {"failure_alert_after": "3"})["failure_alert_after"] == 3
    with pytest.raises(ValueError, match="failure_alert_after"):
        update_job(job["id"], {"failure_alert_after": 0})


def _store_raw(job_id: str, value) -> None:
    """Write *value* straight into the stored record, the way a hand edit of ``jobs.json`` or a
    profile-distribution import does (neither goes through ``update_job``'s validation)."""
    from cron.jobs import load_jobs, save_jobs

    jobs = load_jobs()
    next(j for j in jobs if j["id"] == job_id)["failure_alert_after"] = value
    save_jobs(jobs)


@pytest.mark.parametrize("bad", ["two", 0, -3, "", [2]])
def test_invalid_stored_value_behaves_as_unset(cron_home, sent, caplog, bad):
    import cron.scheduler_signal_retry as retry

    retry._warned_bad_values.clear()
    job = _job(cron_home)
    _store_raw(job["id"], bad)

    with caplog.at_level("WARNING", logger="cron.scheduler"):
        after = _run(cron_home, job["id"], PLAIN_FAILURE)
        assert sent == []  # the sub-daily default (2) withholds the first failure
        assert after["failure_streak"] == 1 and after["last_status"] == "error"

        after = _run(cron_home, job["id"], PLAIN_FAILURE)
    assert len(sent) == 1 and "It has failed 2 runs in a row." in sent[0][1]
    assert after["failure_streak"] == 2
    assert _incident_count(job["id"]) == 1
    invalid = [r for r in caplog.records if "failure_alert_after" in r.getMessage()
               and "is invalid" in r.getMessage()]
    assert len(invalid) == (0 if bad == "" else 1), "one warning per job and bad value"


def test_invalid_stored_value_on_the_crash_path_still_delivers(cron_home, sent):
    import cron.scheduler as sched

    job = {"id": "crash-bad", "name": "c", "no_agent": True, "deliver": "telegram:1",
           "schedule": {"kind": "interval", "minutes": 5}, "failure_alert_after": "nope",
           "failure_streak": 1, "last_failure": {"at": "x", "detail": "RuntimeError: boom"}}
    sched._deliver_crash_failure(job, "RuntimeError: boom", adapters=None, loop=None)

    assert len(sent) == 1 and "It has failed 2 runs in a row." in sent[0][1]


def _cli(*argv: str) -> int:
    """Run ``hermes cron <argv>`` through the real parser and handler."""
    import argparse

    from hermes_cli.cron import cron_command
    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser(prog="hermes")
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=cron_command)
    args = parser.parse_args(["cron", *argv])
    return args.func(args)


def test_cli_create_edit_and_clear_failure_alert_after(cron_home, capsys):
    from cron.jobs import get_job, list_jobs

    (cron_home / "scripts" / "probe.sh").write_text(SILENT_SUCCESS, encoding="utf-8")
    assert _cli("create", "every 5m", "--script", "probe.sh", "--no-agent",
                "--deliver", "local", "--failure-alert-after", "3") == 0
    job_id = list_jobs()[0]["id"]
    assert get_job(job_id)["failure_alert_after"] == 3

    assert _cli("edit", job_id, "--failure-alert-after", "1") == 0
    assert get_job(job_id)["failure_alert_after"] == 1

    assert _cli("edit", job_id, "--failure-alert-after", "0") == 1
    assert "failure_alert_after" in capsys.readouterr().out
    assert get_job(job_id)["failure_alert_after"] == 1

    assert _cli("edit", job_id, "--failure-alert-after", "") == 0
    assert get_job(job_id).get("failure_alert_after") is None  # cleared = the default


def test_tool_update_clears_with_empty_string_and_ignores_null(cron_home):
    import json

    from cron.jobs import get_job
    from tools.registry import registry

    job = _job(cron_home, failure_alert_after=3)

    def call(**args):
        return json.loads(registry.dispatch(
            "cronjob_manage", {"action": "update", "job_id": job["id"], **args}))

    assert call(failure_alert_after=None, name="renamed")["success"] is True
    assert get_job(job["id"])["failure_alert_after"] == 3, "null is 'not sent', never a clear"

    assert call(failure_alert_after="")["success"] is True
    assert get_job(job["id"]).get("failure_alert_after") is None  # cleared = the default

    assert call(failure_alert_after=2)["success"] is True
    assert get_job(job["id"])["failure_alert_after"] == 2


@pytest.mark.parametrize(
    ("schedule", "daily"),
    [
        ({"kind": "cron", "expr": "0 9 * * *"}, True),
        ({"kind": "cron", "expr": "0 9 * * 1-5"}, True),  # 24h and 72h gaps
        ({"kind": "cron", "expr": "0 9 1 * *"}, True),
        ({"kind": "cron", "expr": "0 9,21 * * *"}, False),
        ({"kind": "cron", "expr": "*/30 * * * *"}, False),
        ({"kind": "interval", "minutes": 1440}, True),
        ({"kind": "interval", "minutes": 1439}, False),
        ({"kind": "once", "run_at": "2030-01-01T09:00:00Z"}, False),
    ],
)
def test_runs_daily_or_less_often(schedule, daily):
    from cron.scheduler_signal_retry import _runs_daily_or_less_often

    assert _runs_daily_or_less_often(schedule) is daily
