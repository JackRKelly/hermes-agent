"""A task Jack performs himself is a ``user_task`` (Done), never an ``other`` with Approve (Jack, Nov 13).

Card t_5d7f919e parked "Run this SQL to pause two webhooks" as ``other`` twice, so the app showed
Reject / Snooze / Approve for SQL Jack had to run himself. ``action_item_add`` now refuses an ``other``
or ``design_decision`` whose ask, title or body asks Jack to do it, with a conforming ``user_task``.
"""
from __future__ import annotations

import pytest

from tools.action_item_kinds import LiveSources, SchemaError, build_envelope
from tools.action_item_kinds import readability as rd

HOSTS = {"github.com", "linear.app"}
ENV = {"title": "Pause two dead webhooks", "summary": "Both keep queuing rows nobody sends.",
       "ask": "OK for me to pause both?", "why": "Each holds about 27k unsent rows.",
       "risk": {"level": "low", "note": "Only two rows change."}}
DECISION = {"questions": [{"key": "q1", "question": "Pause both now?", "options": [
    {"key": "both", "label": "Pause both", "recommended": True},
    {"key": "ours", "label": "Only ours", "recommended": False}]}]}
# The two asks and the body the incident card parked as ``other``.
INCIDENT_ASKS = [
    "Run this guarded SQL on prod to pause the two dead Warpspeed charge webhooks, then mark done?",
    "Run the SQL below in a prod Postgres write session, then mark done (or note the error)?",
]
INCIDENT_BODY = ("I can't run this myself: the SQL bot is read-only. Paste the whole block below, from `BEGIN;` to "
                 "`COMMIT;`, into a prod Postgres session with write access.\n\n```sql\nBEGIN;\nCOMMIT;\n```")


def build(kind: str, payload: dict, **overrides) -> dict:
    return build_envelope(kind, payload=payload, sources=LiveSources(), hosts=HOSTS, **{**ENV, **overrides})


def other(body: str = "The pause flips one column on two rows.", **overrides) -> dict:
    return build("other", {"title": "Pause two dead webhooks", "body": body, "links": []}, **overrides)


@pytest.mark.parametrize("ask", INCIDENT_ASKS)
def test_the_incident_asks_are_refused_as_other_with_a_user_task_example(ask):
    with pytest.raises(SchemaError) as refused:
        other(ask=ask)
    message = str(refused.value)
    assert message.startswith("ask: asks Jack to do it himself ('Run"), message
    assert 'kind="user_task"' in message and 'never kind="other" with Approve' in message
    assert '"steps"' in message and '"where"' in message and message.endswith("(see docs/action-items-card-guide.md)")


def test_the_incident_body_is_refused_even_under_a_neutral_ask():
    with pytest.raises(SchemaError, match=r"^payload\.body: asks Jack to do it himself \('Paste'\)"):
        other(INCIDENT_BODY)


@pytest.mark.parametrize("ask", [
    "Run the migration on prod?", "Please paste the token into Railway?", "Jack, sign in to Stripe in the agent browser?",
    "Can you log in to the Shopify admin?", "Could you please click Approve on the consent screen?",
    "Execute the backfill script?", "Upload the signed W-9?", "Call UPS about the claim?", "Sign the MSA from Acme?",
    "Pay the Railway invoice?", "Install the profile on your phone?", "Enter the 2FA code in the agent browser?",
    "Check the readback, then mark this done?",
])
def test_every_self_action_is_refused_on_other_and_design_decision(ask):
    with pytest.raises(SchemaError, match=r"^ask: asks Jack to do it himself"):
        other(ask=ask)
    with pytest.raises(SchemaError, match=r"^ask: asks Jack to do it himself"):
        build("design_decision", DECISION, ask=ask)


def test_the_title_is_checked_too():
    with pytest.raises(SchemaError, match=r"^title: asks Jack to do it himself \('Run'\)"):
        other(title="Run this SQL to pause two webhooks")
    with pytest.raises(SchemaError, match=r"^payload\.title: asks Jack"):
        build("other", {"title": "Run this SQL to pause two webhooks", "body": "x", "links": []})


@pytest.mark.parametrize("ask", [
    "OK for me to run the backfill on prod?", "Should we run the migration tonight?", "Approve the deploy to prod?",
    "Deploy #4368 to prod now?", "Rerun the failed job?", "Running total: keep the old report?", "Signal the worker?",
    "Callback URL: switch to the new host?", "Pause both dead webhooks now?",
])
def test_an_approval_an_agent_carries_out_still_parks_as_other(ask):
    assert other(ask=ask)["kind"] == "other"


@pytest.mark.parametrize("body", [
    "Once approved I run the guarded SQL and read prod back.",
    "1. I run `UPDATE ...` in a read-write job.\n2. I confirm both rows read paused.",
    "The receiver should run fine after this.",
])
def test_an_agent_side_body_is_accepted(body):
    assert other(body)["payload"]["body"] == body


def test_the_same_work_as_a_user_task_is_accepted():
    env = build("user_task", {"steps": ["Open a prod Postgres write session", "Run `UPDATE ... RETURNING id;`"],
                              "where": "Prod Postgres"}, ask=INCIDENT_ASKS[1], title="Run SQL to pause two webhooks")
    assert env["kind"] == "user_task"


def test_other_kinds_are_not_checked():
    # A send's ask is not Jack's own action; the rule is only for other and design_decision.
    assert rd.check_self_task("pr_merge", {}, ask="Run CI again and merge?", title="Merge #1") is None


def test_the_conforming_example_is_a_user_task():
    assert rd.EXAMPLE_USER_TASK["kind"] == "user_task"
    assert rd.EXAMPLE_USER_TASK["payload"]["steps"] and rd.EXAMPLE_USER_TASK["payload"]["where"]
