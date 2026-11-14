"""A ``design_decision`` that says nothing needs Jack is refused at add time, and a free-text park
that says so reads as no questions (tools/action_item_kinds/).

Regression for Nov 13: a stuck-work check that found nothing reached Jack as "Nothing to fix and
nothing needs Jack / What should happen next?".
"""
from __future__ import annotations

import json
import re
from pathlib import Path

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
    ("ask", {"ask": "Not a decision for Jack, just a heads-up."}),
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


@pytest.mark.parametrize("overrides", [
    {"summary": "Should Temu default to UPS or FedEx? Nothing else needs Jack."},
    {"summary": "Checked the deploy settings. Nothing else needs Jack."},
    {"why": "No action needed on staging. Prod: now or Monday?"},
    {"why": "No action needed on staging, but should prod go now or Monday?"},
])
def test_a_real_question_with_a_no_op_phrase_is_accepted(overrides):
    env = build(payload(), **overrides)
    assert env["payload"]["questions"][0]["question"] == "When should staging and prod get it?"


def test_an_ask_in_the_same_sentence_as_the_phrase_is_a_real_question():
    env = build(payload("No action needed on staging, but should prod go now or Monday?"))
    assert env["payload"]["questions"][0]["question"].endswith("now or Monday?")
    projected = legacy_questions("[needs-jack:decision] No action needed on staging, but should prod go now or Monday?")
    assert projected is not None and projected["questions"], projected


def test_a_real_question_ending_in_or_what_is_never_stripped():
    env = build(payload("No action needed on staging. Prod: ship now or what?"))
    assert env["payload"]["questions"][0]["question"].endswith("ship now or what?")
    projected = legacy_questions("[needs-jack:decision] No action needed on staging. Prod: ship now or what?")
    assert projected is not None and projected["questions"], projected


def test_nothing_else_still_refuses_when_no_question_is_real():
    with pytest.raises(SchemaError, match=r"^payload\.questions\[0\]\.question: "):
        build(payload("Nothing else needs Jack; what next?"), summary="Checked it. Nothing else needs Jack.")


RULE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "no_op_rule.v2.json").read_text(encoding="utf-8-sig"))


def test_the_patterns_are_the_shared_fixture():
    """``tests/fixtures/no_op_rule.v2.json`` is committed byte-identical in JackRKelly/assistant."""
    assert RULE["flags"] == "IGNORECASE"
    for name, pattern in (("no_op_re", rd.NO_OP_RE), ("generic_ask_re", rd.GENERIC_ASK_RE),
                          ("ask_left_re", rd.ASK_LEFT_RE), ("url_re", rd.URL_RE)):
        assert pattern.pattern == RULE[name] and pattern.flags & re.IGNORECASE, name


@pytest.mark.parametrize("case", RULE["cases"], ids=lambda c: f"{c['text'][:40]}|{c.get('has_real_question', False)}")
def test_the_shared_fixture_cases(case):
    assert rd.is_no_op_text(case["text"], has_real_question=case.get("has_real_question", False)) is case["no_op"]


def test_a_park_with_a_real_question_and_nothing_else_still_reads_as_its_question():
    reason = "[needs-jack:decision] Pick UPS or FedEx for Temu? Nothing else needs Jack. then=ready"
    projected = legacy_questions(reason)
    assert projected is not None and projected["questions"], projected
    assert legacy_questions("[decision] Pick UPS or FedEx for Temu? Nothing else needs Jack.") is None


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
