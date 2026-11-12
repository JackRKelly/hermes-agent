"""The backend kinds registry (tools/action_item_kinds/): hashes, schemas, fallback, legacy parks.

The §3.2.1 vectors are the shared test vectors of JackRKelly/assistant
``docs/design/action-items-webapp.md`` (and of assistant ``scripts/action_item.py``): both
implementations must reproduce them byte for byte.
"""
from __future__ import annotations

import copy
import json

import pytest

from tools.action_item_kinds import (
    KINDS, KINDS_VERSION, LiveSources, SchemaError, build_envelope, canonical_json, classify,
    comment_body, comment_hash16, item_hash, legacy_payload, park_reason, validate_envelope,
)
from tools.action_item_kinds import send_email, send_message
from tools.action_item_kinds.spec import KINDS_DIR, SourceUnavailable

HOSTS = {"github.com", "linear.app", "mail.google.com", "app.slack.com"}

# The §3.2.1 send_email example, in its one-line form (``\n`` in body decodes to a newline).
EMAIL_VECTOR = (
    '{"kind":"send_email","schema":1,"ask":"Reply to Dave Teeple about meeting Wednesday?",'
    '"why":"Dave (prospect) asked Tue 3:30 AM CT if you have time Wednesday; no reply yet.",'
    '"risk":{"level":"low","note":"External email; draft only, nothing sent until you approve."},'
    '"deadline":"2026-11-11T17:00:00-06:00",'
    '"links":[{"label":"Gmail thread","url":"https://mail.google.com/mail/u/jack@cybership.io/#all/18f3a2b4c5d6e7f8"}],'
    '"then":"complete","source_key":"gmail:jack@cybership.io:18f3a2b4c5d6e7f8",'
    '"payload":{"account":"jack@cybership.io","thread_id":"18f3a2b4c5d6e7f8",'
    '"thread_url":"https://mail.google.com/mail/u/jack@cybership.io/#all/18f3a2b4c5d6e7f8",'
    '"draft_id":"r-1234567890123456789","from":{"name":"Dave Teeple","email":"dave@example.com"},'
    '"last_inbound_at":"2026-11-10T03:30:00-06:00","last_inbound_excerpt":"Might you have time on Wednesday?",'
    '"to":[{"name":"Dave Teeple","email":"dave@example.com"}],"cc":[],"bcc":[],"subject":"Re: Quick chat",'
    '"body":"Hi Dave,\\n\\nWednesday works. Does 2:00 PM CT suit you?\\n\\nJack","attachments":[],'
    '"content_hash":"sha256:88323e8d9715c9a75febf21deadf79c71aa424fe12751e2d51b72c75160500c0"}}'
)


def email_env() -> dict:
    return json.loads(EMAIL_VECTOR)


def message_payload(channel: str = "slack") -> dict:
    if channel == "slack":
        return {"channel": "slack", "account": "T07LES81AAE", "conversation_id": "D07ABC123:1794390000.123456",
                "contact": {"name": "Ann", "handle": "U01ABCDEF"}, "recipients": [{"handle": "U01ABCDEF"}],
                "last_messages": [{"from": "them", "at": "2026-11-10T09:00:00-06:00", "text": "Thursday?"}],
                "last_inbound_at": "2026-11-10T09:00:00-06:00",
                "draft_text": "Yes, Thursday works.\nI'll send the invite.", "draft_ref": None}
    return {"channel": "sms", "account": "this-mac", "conversation_id": "SMS;-;+15125550147",
            "contact": {"handle": "+15125550147"}, "recipients": [{"handle": "+15125550147"}],
            "last_messages": [{"from": "them", "at": "2026-11-10T09:00:00-06:00", "text": "eta?"}],
            "last_inbound_at": "2026-11-10T09:00:00-06:00", "draft_text": "On my way,\r\nsee you at 6",
            "draft_ref": None}


class FakePR(LiveSources):
    def __init__(self, **overrides):
        self.pr = {"state": "OPEN", "title": "fix: Update Product skeleton", "head_sha": "a" * 40,
                   "body": "## TL;DR\n\nThe page **waited** forever. See [CYB-1](https://linear.app/x).\n\n## Root cause\nx",
                   "head_ref": "fix/skeleton", "base_ref": "main", "cross_repo": False, "commits": 1, "files": 2,
                   "mergeable": "MERGEABLE", "merge_state": "BLOCKED", "review_decision": "REVIEW_REQUIRED",
                   "checks": {"total": 3, "green": 3, "pending": 0, "red": 0}, **overrides}

    def github_pr(self, repo, number):
        return dict(self.pr)

    def github_merge_method(self, repo):
        return "squash"


ENVELOPE = {"title": "Merge: product page skeleton fix", "summary": "The product page never loads. This PR fixes it.",
            "ask": "Merge v2#4280 into main?", "why": "The page never loads.",
            "risk": {"level": "low", "note": "UI only."}}
HUMAN = {k: ENVELOPE[k] for k in ("title", "summary")}


def build(kind: str, payload: dict, sources: LiveSources | None = None, **extra) -> dict:
    return build_envelope(kind, payload=payload, sources=sources or LiveSources(), hosts=HOSTS,
                          **{**ENVELOPE, **extra})


# --- the §3.2.1 shared vectors -------------------------------------------------------------------

def test_send_email_vector_hashes_identically_to_the_design_doc():
    env = email_env()
    p = env["payload"]
    assert send_email.content_hash(p["to"], p["cc"], p["bcc"], p["subject"], p["body"], p["attachments"]) == \
        "sha256:88323e8d9715c9a75febf21deadf79c71aa424fe12751e2d51b72c75160500c0"
    assert item_hash(env) == "bfd49d86f87dbc09"
    validate_envelope(env, HOSTS)


def test_send_message_vectors_hash_identically_to_the_design_doc():
    assert send_message.content_hash("slack", "D07ABC123:1794390000.123456", [{"handle": "U01ABCDEF"}],
                                     "Yes, Thursday works.\nI'll send the invite.") == \
        "sha256:5672fb1504668d1fb7a1a9c7625f9af0f799b253419614249b19b53f3da3e0c5"
    assert send_message.content_hash("sms", "SMS;-;+15125550147", [{"handle": "+15125550147"}],
                                     "On my way,\r\nsee you at 6") == \
        "sha256:179daa7553e245fb1b32e2f85ee0c7f8a369935b6b3f79df10551df98322657f"


def test_building_the_vector_from_its_agent_fields_reproduces_the_stored_bytes():
    """The tool path (computed thread_url, content_hash, source_key, default link) writes the doc's
    envelope plus the v2 title and summary, and its park binds that envelope's hash16."""
    vector = email_env()
    agent = {k: v for k, v in vector["payload"].items() if k not in ("thread_url", "content_hash")}
    env = build_envelope("send_email", payload=agent, **HUMAN, ask=vector["ask"], why=vector["why"],
                         risk=vector["risk"], deadline=vector["deadline"], sources=LiveSources(), hosts=HOSTS)
    assert canonical_json({k: v for k, v in env.items() if k not in HUMAN}) == canonical_json(vector)
    assert (env["title"], env["summary"]) == (HUMAN["title"], HUMAN["summary"])
    assert park_reason(env) == (
        "[awaiting-approval:send-email] https://mail.google.com/mail/u/jack@cybership.io/#all/18f3a2b4c5d6e7f8 "
        "draft=r-1234567890123456789 thread=18f3a2b4c5d6e7f8 account=jack@cybership.io then=complete "
        f"item={item_hash(env)}")


def test_hash16_is_over_the_stored_bytes_after_the_17_byte_prefix():
    env = email_env()
    body = comment_body(env)
    assert body.encode("utf-8")[:17] == b"[action-item:v1] "
    assert comment_hash16(body) == item_hash(env) == "bfd49d86f87dbc09"
    # A re-formatted comment (trailing newline, pretty JSON) is different bytes: it never matches.
    assert comment_hash16(body + "\n") != item_hash(env)
    assert comment_hash16("[action-item:v1] " + json.dumps(env, indent=1)) != item_hash(env)


# --- add-time validation: invalid payloads are refused ------------------------------------------

@pytest.mark.parametrize("mutate, field", [
    (lambda e: e["payload"].__setitem__("body", "<b>Hi</b>"), "payload.body"),
    (lambda e: e["payload"].__setitem__("extra", 1), "payload"),
    (lambda e: e["payload"].pop("draft_id"), "payload"),
    (lambda e: e["payload"].__setitem__("subject", "changed"), "payload.content_hash"),
    (lambda e: e.__setitem__("deadline", "2026-11-11T17:00:00"), "deadline"),
    (lambda e: e.__setitem__("then", "ready"), "then"),
    (lambda e: e["links"][0].__setitem__("url", "https://evil.example/x"), "envelope.links[0].url"),
    (lambda e: e.__setitem__("ask", "two\nlines"), "envelope.ask"),
    (lambda e: e.__setitem__("schema", 2), "schema"),
])
def test_an_invalid_frozen_envelope_is_refused(mutate, field):
    env = email_env()
    mutate(env)
    with pytest.raises(SchemaError, match=field.replace(".", r"\.").replace("[", r"\[")):
        validate_envelope(env, HOSTS)


def test_send_message_phone_handles_must_be_e164():
    payload = message_payload("sms")
    payload["recipients"] = [{"handle": "(512) 555-0147"}]
    with pytest.raises(SchemaError, match="E.164"):
        build("send_message", payload, then="complete")
    env = build("send_message", message_payload("sms"), then="complete")
    assert env["source_key"] == "sms:SMS;-;+15125550147"
    assert env["payload"]["content_hash"] == \
        "sha256:179daa7553e245fb1b32e2f85ee0c7f8a369935b6b3f79df10551df98322657f"


def test_pr_merge_tool_fields_come_from_the_live_pr_never_the_agent():
    env = build("pr_merge", {"repo": "cybership-io/v2", "number": 4280, "head_sha": "a" * 40}, FakePR())
    p = env["payload"]
    assert (p["base_ref"], p["head_ref"], p["method"], p["admin"], p["promotion"]) == \
        ("main", "fix/skeleton", "squash", True, False)
    assert p["tldr"] == "The page waited forever. See CYB-1."
    assert park_reason(env).startswith(
        f"[awaiting-approval:merge] https://github.com/cybership-io/v2/pull/4280 head={'a' * 40} base=main "
        "method=squash then=complete item=")
    with pytest.raises(SchemaError, match="filled by the tool"):
        build("pr_merge", {"repo": "cybership-io/v2", "number": 4280, "head_sha": "a" * 40, "base_ref": "dev"},
              FakePR())


@pytest.mark.parametrize("pr, message", [
    ({"head_sha": "b" * 40}, "the PR head is bbbbbbbbbbbb"),
    ({"state": "MERGED"}, "is MERGED, not OPEN"),
])
def test_pr_merge_refuses_a_pr_the_agent_did_not_read(pr, message):
    with pytest.raises(SchemaError, match=message):
        build("pr_merge", {"repo": "cybership-io/v2", "number": 4280, "head_sha": "a" * 40}, FakePR(**pr))


def test_pr_merge_unreadable_source_fails_closed():
    with pytest.raises(SchemaError, match="could not read"):
        build("pr_merge", {"repo": "cybership-io/v2", "number": 4280, "head_sha": "a" * 40}, LiveSources())


def _decision(*options: dict, question: str = "Which?") -> dict:
    return {"questions": [{"key": "q1", "question": question, "options": list(options)}], "rec_reason": "r"}


@pytest.mark.parametrize("kind, payload, message", [
    ("design_decision", _decision({"key": "a", "label": "A", "recommended": True},
                                  {"key": "a", "label": "B", "recommended": False}), "unique"),
    ("design_decision", _decision({"key": "a", "label": "A", "recommended": False},
                                  {"key": "b", "label": "B", "recommended": False}), "exactly one"),
    ("design_decision", _decision({"key": "a", "label": "A", "recommended": True},
                                  {"key": "b", "label": "B", "recommended": False},
                                  question="Pick **one** option?"), "markdown is refused"),
    ("user_task", {"steps": [], "where": ""}, "at least 1"),
    ("other", {"title": "T", "body": "see [x](https://evil.example/)"}, "only https links"),
    ("other", {"title": "T", "body": "<script>x</script>"}, "HTML is refused"),
    ("other", {"title": "T", "body": "ok", "body_hash": "sha256:" + "0" * 64}, "filled by the tool"),
])
def test_invalid_payloads_of_the_new_kinds_are_refused(kind, payload, message):
    with pytest.raises(SchemaError, match=message):
        build(kind, payload)


def test_valid_items_of_every_kind_are_accepted():
    items = {
        "pr_merge": ({"repo": "o/r", "number": 1, "head_sha": "a" * 40}, FakePR()),
        "send_email": ({k: v for k, v in email_env()["payload"].items()}, None),
        "send_message": (message_payload(), None),
        "design_decision": ({"rec_reason": "Cheaper.", "questions": [{"key": "q1", "question": "Refresh the 14 labels?",
            "options": [{"key": "now", "label": "Now", "effect": "Refresh today", "recommended": True},
                        {"key": "later", "label": "Later", "recommended": False}]}]}, None),
        "user_task": ({"steps": ["Sign in to Linear"], "where": "linear.app"}, None),
        "other": ({"title": "Read this", "body": "- **one**\n- `two` [doc](https://github.com/x)"}, None),
    }
    assert set(items) == set(KINDS)
    for kind, (payload, sources) in items.items():
        then = "complete" if kind.startswith("send_") else None
        env = build(kind, payload, sources, then=then)
        view = classify(env, HOSTS)
        assert view["render_as"] == kind and view["fallback"] is None
        assert view["binding_hash"].startswith("sha256:")
        assert comment_hash16(comment_body(env)) in park_reason(env)


def test_user_task_steps_are_stored_without_their_own_number():
    # The exact shape Jack saw as "1. 1. In the agent browser, open ..." (Nov 12): the app numbers the steps.
    steps = ["1. In the agent browser, open https://dashboard.stripe.com/login",
             "2. Sign in to the Cybership Stripe account with \"Remember me on this device\" checked, and finish 2FA.",
             "3) Confirm you can see Workbench > Logs in live mode, then leave the browser open.",
             "- Mark this done.", "2FA stays as written", "1.5 GB is not a list marker"]
    clean = ["In the agent browser, open https://dashboard.stripe.com/login",
             "Sign in to the Cybership Stripe account with \"Remember me on this device\" checked, and finish 2FA.",
             "Confirm you can see Workbench > Logs in live mode, then leave the browser open.",
             "Mark this done.", "2FA stays as written", "1.5 GB is not a list marker"]
    env = build("user_task", {"steps": steps, "where": "The agent browser"})
    assert env["payload"]["steps"] == clean
    # The binding hash covers the stored steps, so the read-time check passes and a re-add is the same item.
    validate_envelope(env, HOSTS)
    again = build("user_task", {"steps": clean, "where": "The agent browser"})
    assert again["payload"]["steps_hash"] == env["payload"]["steps_hash"]


# --- read-time fallback to `other` ---------------------------------------------------------------

def test_unknown_kind_schema_or_payload_on_read_degrades_to_other_with_approve_off():
    unknown = {**email_env(), "kind": "linkedin_post"}
    newer = {**email_env(), "schema": 9}
    broken = email_env()
    broken["payload"]["draft_id"] = 7
    for env, reason in ((unknown, "unknown_kind"), (newer, "schema_version"), (broken, "schema")):
        view = classify(env, HOSTS)
        assert view["render_as"] == "other" and view["fallback"]["reason"] == reason
        assert "approve" not in view["allowed_actions"] and view["binding_hash"] is None
    assert classify(broken, HOSTS)["fallback"]["failed"] == ["payload.draft_id"]
    assert classify("not an object", HOSTS)["render_as"] == "other"


def test_a_promotion_is_never_batchable():
    env = build("pr_merge", {"repo": "o/r", "number": 1, "head_sha": "a" * 40},
                FakePR(head_ref="staging", base_ref="main"))
    assert env["payload"]["promotion"] is True
    assert "batch" not in classify(env, HOSTS)["allowed_actions"]


# --- live_check and legacy_parse --------------------------------------------------------------------

def test_pr_merge_live_check_reports_drift_on_bound_fields():
    env = build("pr_merge", {"repo": "o/r", "number": 1, "head_sha": "a" * 40}, FakePR())
    spec = KINDS["pr_merge"]
    assert spec.live_check(env["payload"], FakePR())["ok"] is True
    drifted = spec.live_check(env["payload"], FakePR(head_sha="c" * 40, base_ref="staging"))
    assert drifted["ok"] is False
    assert drifted["drift"][:2] == ["head moved aaaaaaaa \u2192 cccccccc", "base changed main \u2192 staging"]
    assert spec.live_check(env["payload"], LiveSources())["available"] is False


def test_send_email_live_check_needs_a_draft_reader_and_detects_an_edited_draft():
    payload = email_env()["payload"]

    class Drafts(LiveSources):
        def __init__(self, draft):
            self.draft = draft

        def gmail_draft(self, account, draft_id):
            return self.draft

    spec = KINDS["send_email"]
    assert spec.live_check(payload, LiveSources())["available"] is False
    assert spec.live_check(payload, Drafts(copy.deepcopy(payload)))["ok"] is True
    assert spec.live_check(payload, Drafts({**payload, "body": "edited"}))["drift"] == ["draft changed"]
    assert spec.live_check(payload, Drafts(None))["drift"] == ["draft is gone"]


def test_legacy_parks_are_typed_from_their_reason():
    head = "a" * 40
    assert legacy_payload(f"[awaiting-approval:merge] https://github.com/o/r/pull/12 head={head} then=complete") == \
        ("pr_merge", {"repo": "o/r", "number": 12, "head_sha": head})
    kind, payload = legacy_payload("[awaiting-approval:send-email] https://x draft=r-1 thread=t1 account=a@b.co")
    assert kind == "send_email" and payload["draft_id"] == "r-1"
    assert legacy_payload("[needs-jack:sign-in] Sign in to Linear")[0] == "user_task"
    kind, payload = legacy_payload("[awaiting-approval:deploy] Deploy the staged change then=ready")
    assert kind == "other" and payload["title"].startswith("Approve deploy")
    assert legacy_payload("") is None


# --- the registry contract -------------------------------------------------------------------------

def test_every_kind_ships_its_schema_and_a_complete_spec():
    assert isinstance(KINDS_VERSION, int) and KINDS_VERSION >= 1
    assert {"pr_merge", "send_email", "send_message", "design_decision", "user_task", "other"} <= set(KINDS)
    for kind, spec in KINDS.items():
        for version in spec.schema_versions:
            assert (KINDS_DIR / f"{kind}.v{version}.schema.json").is_file()
        # A binding field is read from every stored version, so it is a field of at least one of them.
        assert set(spec.binding_fields) <= {p for s in spec.schema_versions.values() for p in s["properties"]}
        assert spec.agent_fields <= set(spec.schema_versions[spec.current_schema]["properties"])
        for fn in (spec.enrich, spec.live_check, spec.legacy_parse):
            assert callable(fn)


def test_schemas_are_valid_draft_2020_12_and_agree_with_jsonschema():
    """The subset validator is the gate; a standard validator must accept what it accepts."""
    jsonschema = pytest.importorskip("jsonschema")
    from tools.action_item_kinds.registry import ENVELOPE_SCHEMA

    validator = jsonschema.Draft202012Validator
    for path in sorted(KINDS_DIR.glob("*.schema.json")):
        validator.check_schema(json.loads(path.read_text()))
    env = email_env()
    validator(ENVELOPE_SCHEMA).validate(env)
    validator(KINDS["send_email"].schema_versions[1]).validate(env["payload"])
    env["payload"]["surprise"] = 1
    assert not validator(KINDS["send_email"].schema_versions[1]).is_valid(env["payload"])


def test_source_unavailable_is_a_runtime_error_not_a_schema_error():
    assert not issubclass(SourceUnavailable, ValueError)
