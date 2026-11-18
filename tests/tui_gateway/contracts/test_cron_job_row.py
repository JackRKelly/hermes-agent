"""``CronJobRow`` declares every key ``_format_job`` can return.

``CronJobRow`` is an open model, so an undeclared key still passes validation as an extra field and
never reaches the generated TypeScript type: the drift is silent. This pins the contract to the
producer by formatting a job with every optional field set and checking the result's keys.
"""

from __future__ import annotations

from tools.cronjob_job_args import _FORMAT_JOB_OPTIONAL_KEYS, _format_job
from tui_gateway.contracts.tools_commands import CronJobRow

# A value for every optional key, so each one is echoed (``_format_job`` drops falsy ones).
_OPTIONAL_VALUES = {
    "script": "collect.py",
    "reasoning_effort": "high",
    "monitor_script": "watch.py",
    "monitor_url": "https://example.invalid/status",
    "monitor_quiet_empty": True,
    "monitor_state": {"last_hash": "abc"},
    "no_agent": True,
    "enabled_toolsets": ["web"],
    "workdir": "/tmp/project",
    "interpreter": "/usr/bin/python3",
}


def _full_job() -> dict:
    return {
        "id": "job-1",
        "name": "daily",
        "prompt": "summarise",
        "skills": ["brief"],
        "model": "some-model",
        "provider": "some-provider",
        "schedule_display": "every 1d",
        "last_error": "boom",
        "last_delivery_unverified": ["telegram:1"],
        "context_from": ["self", "job-0"],
        "attach_to_session": True,
        **_OPTIONAL_VALUES,
    }


def test_every_format_job_key_is_declared():
    assert set(_FORMAT_JOB_OPTIONAL_KEYS) <= set(_OPTIONAL_VALUES), "give the new optional key a value"
    row = _format_job(_full_job())
    undeclared = sorted(set(row) - set(CronJobRow.model_fields))
    assert not undeclared, f"declare {undeclared} on CronJobRow"


def test_full_row_validates_against_declared_types():
    row = CronJobRow.model_validate(_format_job(_full_job()))
    assert row.monitor_quiet_empty is True
    assert not row.model_extra
