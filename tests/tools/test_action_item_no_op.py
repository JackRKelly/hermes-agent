"""A ``design_decision`` that says nothing needs Jack is refused at add time, and a free-text park
that says so reads as no questions (tools/action_item_kinds/).

Regression for Nov 13: a stuck-work check that found nothing reached Jack as "Nothing to fix and
nothing needs Jack / What should happen next?".
"""
from __future__ import annotations

import pytest

from tools.action_item_kinds import LiveSources, SchemaError, build_envelope, legacy_payload, legacy_questions
from tools.action_item_kinds import readability as rd

HOSTS = {"github.com", "linear.app", "mail.google.com", "app.slack.com"}
ENV = {"title": "Railway: when to roll out staging and prod?",
       "summary": "Dev now uses the new deploy settings. Staging and prod need the same change before Dec 1.",
       "ask": "Pick the rollout timing.", "why": "After Dec 1 the next prod deploy changes Redis builds.",
       "risk": {"level": "medium", "note": "Waiting past Dec 1 breaks the next prod deploy."}}
NO_OP_MESSAGE = ("This says nothing needs Jack: complete the card with a one-line summary instead, or tag it "
                 "[captain:<topic>] for Captain triage")


def payload(question: str = "When should staging and prod get it?", options: list | None = None, **extra) -> dict:
    return {"questions": [{"key": "q1", "question": question, "options": options if options is not None else [
        {"key": "staging-first", "label": "Staging this week, then prod", "recommended": True},
        {"key": "both-now", "label": "Both this week", "recommended": False}]}], **extra}


def build(p: dict, **overrides) -> dict:
    return build_envelope("design_decision", payload=p, sources=LiveSources(), hosts=HOSTS, **{**ENV, **overrides})


@pytest.mark.parametrize("where, call", [
    ("summary", {"summary": "Nothing to fix and nothing needs Jack."}),
    ("payload.questions[0].question", {"p": payload("Nothing here needs Jack; what next?")}),
    ("title", {"title": "No decision needed for the deploy"}),
    ("ask", {"ask": "Not a decision for Jack, just close it?"}),
    ("why", {"why": "Nothing to do here."}),
    ("payload.rec_reason", {"p": payload(rec_reason="No action is required.")}),
])
def test_a_decision_that_says_nothing_needs_jack_is_refused_with_the_fix(where, call):
    p = call.pop("p", payload())
    with pytest.raises(SchemaError) as refused:
        build(p, **call)
    message = str(refused.value)
    assert message.startswith(f"{where}: {NO_OP_MESSAGE}. Write e.g. "), message


def test_a_normal_decision_is_accepted():
    env = build(payload(rec_reason="Staging first catches a bad setting before prod."))
    assert env["kind"] == "design_decision" and env["payload"]["questions"][0]["key"] == "q1"


def test_a_question_with_no_options_is_refused():
    with pytest.raises(SchemaError, match=r"payload\.questions\[0\]\.options: 0 options, needs 2-4"):
        build(payload(options=[]))


def test_plain_decision_words_do_not_trip_the_no_op_rule():
    for text in ("Nothing breaks if we wait a week", "Should Jack fix the deploy now?", "No rush, decide Monday",
                 "Do nothing to the labels?"):
        assert rd.NO_OP_RE.search(text) is None, text


@pytest.mark.parametrize("reason", [
    "[decision] Consult Jack: Nothing to fix and nothing needs Jack. (from t_x)",
    "[needs-jack:decision] Consult Jack: Nothing to fix and nothing needs Jack. then=ready",
])
def test_a_no_op_park_reads_as_no_questions(reason):
    projected = legacy_questions(reason)
    assert projected is not None and projected["questions"] == []
    kind, parsed = legacy_payload(reason)
    assert kind == "design_decision" and parsed["questions"] == []


def test_a_real_needs_jack_park_still_reads_as_its_question():
    assert legacy_questions("[needs-jack:decision] Consult Jack: pick a vendor then=ready")["questions"][0][
        "question"] == "Consult Jack: pick a vendor"
    assert legacy_questions("[decision] Pick a vendor") is None
