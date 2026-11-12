"""No local path is printed on an Action Item (Jack, Nov 12), except a file the card shows.

LinkedIn card t_f587f571 parked an ``other`` item whose body named two PNGs in its workspace by
absolute path, and the add went through because the local-path rule only covered the title, summary,
questions and option labels. Every human-facing text now refuses a local path, except a file inside
the card's own kanban workspace or LinkedIn preview dir (the app embeds it). That exception depends on
the card, so the tool passes the card it resolved; a call with no card refuses every local path.
"""
from __future__ import annotations

import pytest

from tests.tools.test_action_item_tools import as_worker, board, call, card_state, make_card  # noqa: F401
from tools.action_item_kinds import LiveSources, SchemaError, build_envelope, classify

HOSTS = {"github.com", "linear.app"}
GUIDE_TAIL = "(see docs/action-items-card-guide.md)"
CARD = "t_f587f571"
ENV = {"title": "LinkedIn post: age-based storage billing", "summary": "The post is ready for the Tuesday slot.",
       "ask": "Post it as written?", "why": "The slot is Tuesday 11am.",
       "risk": {"level": "low", "note": "Public post on Jack's account."}}
DECISION = {"questions": [{"key": "q1", "question": "Convoy: keep or discard?", "options": [
    {"key": "discard", "label": "Discard it", "recommended": True, "effect": "Nothing stops."},
    {"key": "keep", "label": "Keep it", "recommended": False}]}], "rec_reason": "It isn't running."}


@pytest.fixture
def root(monkeypatch, tmp_path):
    """The shared board root the app serves card files from (``HERMES_KANBAN_HOME``)."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    return tmp_path


def post_body(*images) -> str:
    return ("Account: jack\nSlot: 2026-11-18T11:00-06:00\nExact text:\nWe're building age-based storage billing.\n"
            + "".join(f"Image: {image}\n" for image in images))


def build(kind: str, payload: dict, card=None, **overrides) -> dict:
    return build_envelope(kind, payload=payload, sources=LiveSources(), hosts=HOSTS, card=card, **{**ENV, **overrides})


def other(body: str, card=None, **overrides) -> dict:
    return build("other", {"title": "LinkedIn post", "body": body, "links": []}, card=card, **overrides)


def test_the_linkedin_body_with_its_own_workspace_images_is_accepted(root):
    workspace = root / "kanban" / "workspaces" / CARD
    previews = root / "state" / "linkedin" / "previews" / CARD
    body = post_body(workspace / "post-image.png", workspace / "preview2.png", previews / "p.png")
    env = other(body, card=CARD)
    assert env["payload"]["body"] == body and classify(env, HOSTS)["fallback"] is None


@pytest.mark.parametrize("card", [None, "t_00000000"])
def test_the_same_body_on_another_card_or_no_card_is_refused(root, card):
    image = root / "kanban" / "workspaces" / CARD / "post-image.png"
    with pytest.raises(SchemaError) as refused:
        other(post_body(image), card=card)
    message = str(refused.value)
    assert message.startswith(f"payload.body: contains a local path ({str(image)!r})"), message
    assert "#n" in message and "backticks" in message and message.endswith(GUIDE_TAIL)


@pytest.mark.parametrize("path", [
    "/Users/jack/.hermes/kanban/workspaces/t_f587f571/post-image.png",  # the live root, not this board's
    "{ws}/../t_00000000/post-image.png",  # out of the workspace by ..
    "{ws}",  # the workspace itself, not a file in it
    "{root}/kanban/workspaces/t_f587f5710/x.png",  # a sibling whose name starts with the card id
    "~/notes.md", "/private/var/folders/x/report.md", "/tmp/out.log", ".hermes/kanban.db",
])
def test_any_other_local_path_is_refused_even_on_the_card(root, path):
    raw = path.format(ws=root / "kanban" / "workspaces" / CARD, root=root)
    with pytest.raises(SchemaError, match=r"^payload\.body: contains a local path"):
        other(f"See `{raw}` for the details.", card=CARD)


@pytest.mark.parametrize("kind, payload, overrides, where", [
    ("other", {"title": "Report in /Users/jack/r.md", "body": "x", "links": []}, {}, "payload.title"),
    ("other", {"title": "t", "body": "x", "links": []}, {"ask": "Read /Users/jack/r.md?"}, "ask"),
    ("other", {"title": "t", "body": "x", "links": []}, {"why": "See ~/r.md."}, "why"),
    ("other", {"title": "t", "body": "x", "links": []},
     {"risk": {"level": "low", "note": "Logs in /tmp/x.log"}}, "risk.note"),
    ("user_task", {"steps": ["1. Open ~/.hermes/.env", "2. Save"], "where": "Mac"}, {}, "payload.steps[0]"),
    ("user_task", {"steps": ["Sign in"], "where": "/Users/jack/Desktop"}, {}, "payload.where"),
    ("design_decision", {**DECISION, "rec_reason": "Report: /Users/jack/r.md"}, {}, "payload.rec_reason"),
    ("design_decision", {**DECISION, "questions": [{**DECISION["questions"][0], "options": [
        {"key": "discard", "label": "Discard it", "recommended": True, "effect": "Frees /private/var/x"},
        {"key": "keep", "label": "Keep it", "recommended": False}]}]}, {}, "payload.questions[0].options[0].effect"),
])
def test_every_human_facing_text_refuses_a_local_path_with_its_field(root, kind, payload, overrides, where):
    with pytest.raises(SchemaError) as refused:
        build(kind, payload, card=CARD, **overrides)
    message = str(refused.value)
    assert message.startswith(f"{where}: contains a local path"), message
    assert f'Write e.g. "Image: {root / "kanban" / "workspaces" / CARD}/post-image.png"' in message
    assert message.endswith(GUIDE_TAIL)


@pytest.mark.parametrize("text", [
    "See https://github.com/cybership-io/v2/blob/main/tmp/home/x.md and v2#4194.",
    "The fix is in `src/home/page.tsx` and `apps/web/opt/flags.ts`.",
    "Run `redis-cli KEYS 'bull:*'` against `REDIS_PUBLIC_URL`.",
    "```bash\npnpm --filter api exec vitest run src/var/a.test.ts\n```",
])
def test_urls_repo_paths_and_code_pass(root, text):
    assert other(text, card=CARD)["payload"]["body"] == text


def test_a_send_draft_is_the_message_and_is_not_checked_here(root):
    """A send_email body is the outgoing email, not text about the card: the path rule leaves it alone."""
    from tools.action_item_kinds.readability import _payload_texts
    assert _payload_texts("send_email", {"body": "/Users/jack/x"}) == []
    assert _payload_texts("send_message", {"draft_text": "/Users/jack/x"}) == []


# --- through the tool: the card the worker runs is the one whose files it may name -----------------

def _post(image) -> dict:
    return {"kind": "other", "payload": {"title": "LinkedIn post", "body": post_body(image), "links": []}, **ENV}


def test_the_tool_accepts_its_own_cards_image_and_refuses_another_cards(board, monkeypatch):  # noqa: F811
    from hermes_cli.kanban_db import kanban_home
    tid, run_id = make_card("linkedin-captain", claim=True)
    as_worker(monkeypatch, "linkedin-captain", tid, run_id)
    workspaces = kanban_home() / "kanban" / "workspaces"

    refused = call("action_item_add", **_post(workspaces / "t_00000000" / "post-image.png"))
    assert "payload.body: contains a local path" in refused["error"] and "Nothing was written" in refused["error"]
    assert card_state(tid)[0] == "running" and card_state(tid)[1] == []

    added = call("action_item_add", **_post(workspaces / tid / "post-image.png"))
    assert added.get("ok") is True, added
    assert card_state(tid)[0] == "blocked"
