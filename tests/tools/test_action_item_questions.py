"""KINDS_VERSION 2: multi-question ``design_decision``, the required title and summary, the
readability rules and the legacy ``[needs-jack:`` park reader (tools/action_item_kinds/).

The limits and the banned list are the ones in JackRKelly/assistant ``docs/action-items-card-guide.md``.
"""
from __future__ import annotations

import json

import pytest

from tools.action_item_kinds import (
    KINDS, LiveSources, SchemaError, build_envelope, classify, legacy_payload, legacy_questions,
    prefixed_hash, questions_of, validate_envelope,
)
from tools.action_item_kinds import readability as rd
from tools.action_item_kinds.registry import ENVELOPE_SCHEMA

HOSTS = {"github.com", "linear.app", "mail.google.com", "app.slack.com"}
HUMAN = {"title": "Railway: when to roll out staging and prod?",
         "summary": "Dev now uses the new deploy settings. Staging and prod need the same change before Dec 1."}
ENV = {**HUMAN, "ask": "Pick the rollout timing and what to do with Convoy.",
       "why": "After Dec 1 the next prod deploy changes Redis builds.",
       "risk": {"level": "medium", "note": "Waiting past Dec 1 breaks the next prod deploy."}}
GUIDE_TAIL = "(see docs/action-items-card-guide.md)"

# The [needs-jack:railway-staging-prod] park reason of t_ee8ed102, verbatim.
RAILWAY_PARK = (
    "[needs-jack:railway-staging-prod] Dev is done: Pyroscope, Redis and Slack Bot are off railway.json there "
    "and healthy. Prod and staging staged changes are discarded, so both are untouched. Report: "
    "/Users/jack/.hermes/kanban/workspaces/t_ee8ed102/railway-dev-rollout-report.md. Two decisions: (1) When "
    "should staging, then prod, get the same 3-service change? It must happen before 2026-12-01. After that "
    "date, the next deploy of prod Redis builds with NIXPACKS, and Slack Bot and Pyroscope lose their "
    "healthchecks. (2) Convoy: keep or discard? It isn't running anywhere. Rec: staging this week, then prod; "
    "discard Convoy. then=ready"
)


def questions(*qs: dict) -> dict:
    return {"questions": list(qs), "rec_reason": "Staging first catches a bad setting before prod."}


def q(key: str = "q1", question: str = "Convoy: keep or discard?", options: list | None = None, **extra) -> dict:
    return {"key": key, "question": question, "options": options if options is not None else [
        {"key": "discard", "label": "Discard it", "recommended": True,
         "effect": "Convoy isn't running anywhere, so nothing stops."},
        {"key": "keep", "label": "Keep it", "recommended": False}], **extra}


def build(payload: dict, **overrides) -> dict:
    return build_envelope("design_decision", payload=payload, sources=LiveSources(), hosts=HOSTS,
                          **{**ENV, **overrides})


# --- v2 multi-question ---------------------------------------------------------------------------

def test_a_multi_question_decision_validates_and_is_answered_never_approved():
    env = build(questions(q("q1", "When should staging and prod get it?", [
        {"key": "both-now", "label": "Both this week", "recommended": False},
        {"key": "staging-first", "label": "Staging this week, then prod", "recommended": True}]),
        q("q2", allow_other=False)))
    assert env["schema"] == 2 and env["title"] == HUMAN["title"] and env["summary"] == HUMAN["summary"]
    assert [x["allow_other"] for x in env["payload"]["questions"]] == [True, False]  # default true
    view = classify(env, HOSTS)
    assert view["render_as"] == "design_decision" and view["fallback"] is None
    assert "answer" in view["allowed_actions"]
    assert not {"approve", "reject", "pick_option"} & set(view["allowed_actions"])
    assert not {"approve", "reject", "pick_option"} & set(KINDS["design_decision"].allowed_actions)
    normalized = questions_of(env["payload"])
    assert [o["key"] for o in normalized[0]["options"]] == ["staging-first", "both-now"]  # recommended first


def test_option_set_hash_covers_every_question():
    base = questions(q("q1"), q("q2", "When should staging and prod get it?"))
    env = build(base)
    reworded = json.loads(json.dumps(base))
    reworded["questions"][1]["options"][1]["label"] = "Keep it running"
    other = build(reworded)
    assert env["payload"]["option_set_hash"] != other["payload"]["option_set_hash"]
    assert classify(env, HOSTS)["binding_hash"] != classify(other, HOSTS)["binding_hash"]


@pytest.mark.parametrize("payload, message", [
    (questions(q("q1"), q("q1")), "question keys must be unique"),
    (questions(q(options=[{"key": "other", "label": "A", "recommended": True},
                          {"key": "b", "label": "B", "recommended": False}])), "reserved"),
    (questions(q(options=[{"key": "a", "label": "A", "recommended": True},
                          {"key": "b", "label": "B", "recommended": True}])), "exactly one"),
    (questions(q(key="q9")), "payload.questions[0].key"),
])
def test_structural_rules_of_each_question(payload, message):
    with pytest.raises(SchemaError, match=message.replace("[", r"\[").replace(".", r"\.")):
        build(payload)


# --- v1 still reads -------------------------------------------------------------------------------

V1_PAYLOAD = {"question": "Refresh the 14 labels?", "rec_reason": "Cheaper.", "options": [
    {"key": "later", "label": "Later", "effect": "Wait", "pros": [], "cons": ["slow"], "recommended": False},
    {"key": "now", "label": "Now", "effect": "Refresh today", "pros": ["fast"], "cons": [], "recommended": True}]}


def v1_envelope() -> dict:
    """A design_decision item as phase 1a stored it: envelope without title or summary, payload v1."""
    payload = {**V1_PAYLOAD, "option_set_hash": prefixed_hash(
        {"question": V1_PAYLOAD["question"], "options": V1_PAYLOAD["options"]})}
    return {"kind": "design_decision", "schema": 1, "ask": "Refresh the 14 labels now?", "why": "Two are stale.",
            "risk": {"level": "low", "note": "x" * 180}, "deadline": None, "links": [], "then": "ready",
            "payload": payload}


def test_a_v1_item_still_reads_keeps_its_binding_and_normalizes():
    env = v1_envelope()
    validate_envelope(env, HOSTS)
    view = classify(env, HOSTS)
    assert view["render_as"] == "design_decision" and view["fallback"] is None
    # The binding hash a v1 item was approved under does not move.
    assert view["binding_hash"] == prefixed_hash({"kind": "design_decision", "schema": 1, "binding": {
        "question": V1_PAYLOAD["question"], "option_set_hash": env["payload"]["option_set_hash"]}})
    [only] = questions_of(env["payload"])
    assert only["key"] == "q1" and only["question"] == "Refresh the 14 labels?" and only["allow_other"] is True
    assert [o["key"] for o in only["options"]] == ["now", "later"]
    assert set(only["options"][0]) == {"key", "label", "effect", "recommended"}


def test_new_items_are_written_as_v2_and_the_v1_shape_is_refused_with_its_fix():
    with pytest.raises(SchemaError) as refused:
        build({k: v for k, v in V1_PAYLOAD.items()})
    message = str(refused.value)
    assert message.startswith("payload.questions: required; design_decision asks its questions as questions[]")
    assert '"questions": [{"key": "q1"' in message and message.endswith(GUIDE_TAIL)


def test_several_numbered_questions_in_one_question_are_refused_with_split():
    text = "(1) When should staging get it? (2) Convoy: keep or discard?"
    with pytest.raises(SchemaError) as refused:
        build(questions(q(question=text)))
    message = str(refused.value)
    assert message.startswith("payload.questions[0].question: holds 2 numbered questions; split into questions[]")
    assert message.endswith(GUIDE_TAIL)
    with pytest.raises(SchemaError, match=r"split into questions\[\]"):
        build({"question": text, "options": []})


# --- title, summary, risk.note: required and limited at add time ---------------------------------

def test_the_title_length_refusal_names_the_rule_and_a_conforming_example():
    with pytest.raises(SchemaError) as refused:
        build(questions(q()), title="When should staging and then prod both get the same change")
    assert str(refused.value) == ('title: 11 words, max 8. Write e.g. "Railway: when to roll out staging '
                                  'and prod?" (see docs/action-items-card-guide.md)')


def test_the_banned_tag_refusal_names_the_token_and_a_conforming_example():
    with pytest.raises(SchemaError) as refused:
        build(questions(q()), title="[needs-jack:railway] Railway rollout")
    message = str(refused.value)
    assert message.startswith("title: contains a raw tag ('[needs-jack:')")
    assert 'Write e.g. "Railway: when to roll out staging and prod?"' in message and message.endswith(GUIDE_TAIL)


@pytest.mark.parametrize("field, value, where, problem", [
    ("title", None, "title", "required"),
    ("title", "  ", "title", "required"),
    ("title", "one\ntwo", "title", "must be one line"),
    ("title", "Supercalifragilistic expialidocious rollout timing and Convoy", "title", "61 chars, max 60"),
    ("summary", None, "summary", "required"),
    ("summary", "One. Two. Three.", "summary", "3 sentences, max 2"),
    ("summary", "x" * 241, "summary", "241 chars, max 240"),
    ("risk", {"level": "low", "note": "x" * 121}, "risk.note", "121 chars, max 120"),
    ("risk", {"level": "low", "note": "one\ntwo"}, "risk.note", "must be one line"),
])
def test_each_envelope_limit_is_refused_with_the_field_name(field, value, where, problem):
    with pytest.raises(SchemaError) as refused:
        build(questions(q()), **{field: value})
    message = str(refused.value)
    assert message.startswith(f"{where}: ") and problem in message and message.endswith(GUIDE_TAIL), message


@pytest.mark.parametrize("payload, where, problem", [
    (questions(), "payload.questions", "0 questions, needs 1-5"),
    (questions(*(q(f"q{i}") for i in range(1, 7))), "payload.questions", "6 questions, needs 1-5"),
    (questions(q(question="x" * 121)), "payload.questions[0].question", "121 chars, max 120"),
    (questions(q(options=[{"key": "a", "label": "A", "recommended": True}])), "payload.questions[0].options",
     "1 options, needs 2-4"),
    (questions(q(options=[{"key": k, "label": k, "recommended": k == "a"} for k in "abcde"])),
     "payload.questions[0].options", "5 options, needs 2-4"),
    (questions(q(options=[{"key": "a", "label": "x" * 41, "recommended": True},
                          {"key": "b", "label": "B", "recommended": False}])),
     "payload.questions[0].options[0].label", "41 chars, max 40"),
    (questions(q(options=[{"key": "a", "label": "A", "effect": "x" * 121, "recommended": True},
                          {"key": "b", "label": "B", "recommended": False}])),
     "payload.questions[0].options[0].effect", "121 chars, max 120"),
    ({**questions(q()), "rec_reason": "x" * 241}, "payload.rec_reason", "241 chars, max 240"),
    ({"rec_reason": "r"}, "payload.questions", "required"),
])
def test_each_decision_limit_is_refused_with_the_field_name(payload, where, problem):
    with pytest.raises(SchemaError) as refused:
        build(payload)
    message = str(refused.value)
    assert message.startswith(f"{where}: ") and problem in message and message.endswith(GUIDE_TAIL), message


BANNED_SAMPLES = [
    ("[needs-jack:x]", "[needs-jack:"), ("[Captain]", "[Captain]"), ("then=ready", "then="),
    ("HEAD=abc", "HEAD="), ("draft=r-1", "draft="), ("thread=t1", "thread="), ("account=a", "account="),
    ("see t_ee8ed102", "t_ee8ed102"), ("/Users/jack/x", "/Users/"), ("~/notes", "~/"),
    ("/private/var", "/private/"), ("in .hermes/kanban", ".hermes/"), ("fast \u2014 now", "\u2014"),
    ("Needs-Jack", "Needs-Jack"), ("awaiting-approval", "awaiting-approval"), ("a legacy park", "legacy park"),
    ("then unblock it", "unblock"), ("hands the card back", "hands the card back"),
]


def _with_text(where: str, text: str) -> dict:
    if where == "title":
        return {"payload": questions(q()), "title": f"Railway {text}"}
    if where == "summary":
        return {"payload": questions(q()), "summary": f"Railway {text}."}
    if where == "question":
        return {"payload": questions(q(question=f"Railway {text}?"))}
    return {"payload": questions(q(options=[{"key": "a", "label": f"Go {text}", "recommended": True},
                                            {"key": "b", "label": "B", "recommended": False}]))}


FIELDS = {"title": "title", "summary": "summary", "question": "payload.questions[0].question",
          "label": "payload.questions[0].options[0].label"}


@pytest.mark.parametrize("text, token", BANNED_SAMPLES)
@pytest.mark.parametrize("where", sorted(FIELDS))
def test_each_banned_token_is_refused_in_every_human_field(where, text, token):
    call = _with_text(where, text)
    with pytest.raises(SchemaError) as refused:
        build(call.pop("payload"), **call)
    message = str(refused.value)
    assert message.startswith(f"{FIELDS[where]}: contains ") and repr(token) in message, message
    assert message.endswith(GUIDE_TAIL)


def test_plain_words_pass_the_banned_list():
    for text in ("Railway: when to roll out staging and prod?", "Head of sales asks", "t_ee8ed10 is short",
                 "Then ready", "the doc link [1]", "users/notes"):
        assert rd.banned_token(text) is None, text


def test_the_schemas_carry_the_same_limits_as_the_readability_module():
    """The read-time caps and the add-time rules must not drift apart."""
    props = ENVELOPE_SCHEMA["properties"]
    assert props["title"]["maxLength"] == rd.TITLE_MAX_CHARS
    assert props["summary"]["maxLength"] == rd.SUMMARY_MAX_CHARS
    assert props["risk"]["properties"]["note"]["maxLength"] >= rd.RISK_NOTE_MAX_CHARS  # v1 notes stay readable
    v2 = KINDS["design_decision"].schema_versions[2]
    question, option = v2["$defs"]["question"]["properties"], v2["$defs"]["option"]["properties"]
    assert (v2["properties"]["questions"]["minItems"], v2["properties"]["questions"]["maxItems"]) == \
        (1, rd.MAX_QUESTIONS)
    assert question["question"]["maxLength"] == rd.QUESTION_MAX_CHARS
    assert (question["options"]["minItems"], question["options"]["maxItems"]) == (rd.MIN_OPTIONS, rd.MAX_OPTIONS)
    assert option["label"]["maxLength"] == rd.OPTION_LABEL_MAX_CHARS
    assert option["effect"]["maxLength"] == rd.OPTION_EFFECT_MAX_CHARS
    assert v2["properties"]["rec_reason"]["maxLength"] == rd.REC_REASON_MAX_CHARS


def test_the_conforming_examples_pass_their_own_rules():
    env = build(questions(*rd.EXAMPLE_QUESTIONS), title=rd.EXAMPLE_TITLE, summary=rd.EXAMPLE_SUMMARY,
                risk={"level": "low", "note": rd.EXAMPLE_RISK_NOTE})
    assert classify(env, HOSTS)["fallback"] is None


# --- legacy [needs-jack: parks ---------------------------------------------------------------------

def test_the_railway_park_reads_as_two_questions_with_the_recommended_answers_first():
    kind, payload = legacy_payload(RAILWAY_PARK)
    assert kind == "design_decision" and payload == legacy_questions(RAILWAY_PARK)
    q1, q2 = payload["questions"]
    assert (q1["key"], q1["question"]) == ("q1", "When should staging, then prod, get the same 3-service change?")
    assert [(o["label"], o["recommended"]) for o in q1["options"]] == [("Staging this week, then prod", True)]
    assert (q2["key"], q2["question"]) == ("q2", "Convoy: keep or discard?")
    assert [(o["label"], o["recommended"]) for o in q2["options"]] == [("Discard", True), ("Keep", False)]
    assert q1["allow_other"] is True and q2["allow_other"] is True
    assert payload["source_text"] == RAILWAY_PARK
    shown = " ".join([x["question"] for x in payload["questions"]]
                     + [o["label"] for x in payload["questions"] for o in x["options"]])
    assert rd.banned_token(shown) is None and "Rec:" not in shown


def test_an_unparseable_needs_jack_park_is_one_free_text_question():
    kind, payload = legacy_payload("[needs-jack:decision] Consult Jack: pick a vendor then=ready")
    assert kind == "design_decision"
    assert payload["questions"] == [{"key": "q1", "question": "Consult Jack: pick a vendor", "options": [],
                                     "allow_other": True}]


def test_a_legacy_park_never_yields_an_approve_style_option():
    for reason in ("[needs-jack:deploy] Approve or reject the staged deploy?",
                   "[needs-jack:deploy] Ship the staged deploy? Rec: approve it"):
        payload = legacy_questions(reason)
        labels = [o["label"].lower() for x in payload["questions"] for o in x["options"]]
        assert not any(label.startswith(("approve", "reject")) for label in labels), labels
        assert all(x["allow_other"] for x in payload["questions"])


def test_an_a_or_b_question_offers_both_and_numbered_dot_lists_split():
    payload = legacy_questions("[needs-jack:x] 1. Default to UPS or FedEx for label buys? 2. Refresh now or later? "
                               "Rec: UPS; later")
    q1, q2 = payload["questions"]
    assert [(o["label"], o["recommended"]) for o in q1["options"]] == [("UPS", True), ("FedEx", False)]
    assert [(o["label"], o["recommended"]) for o in q2["options"]] == [("Later", True), ("Now", False)]


def test_only_needs_jack_parks_are_read_as_questions():
    assert legacy_questions("[awaiting-approval:deploy] staged change then=ready") is None
    assert legacy_questions("") is None
    assert legacy_payload("[needs-jack:sign-in] Sign in to Linear")[0] == "user_task"
