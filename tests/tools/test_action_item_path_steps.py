"""A user_task step may carry one full path in its only code span; a folder-by-folder step is refused.

Jack, Nov 14: a Full Disk Access task named its path folder by folder ("Open `.hermes`, then `tools`,
then ..., then `bin`") and he could not follow it, because ``action_item_add`` refused the full
``~/.hermes/...`` path in every step. A step's sole code span is what Jack pastes (the app renders it
raw and never resolves it as a file), so a path there is allowed; everywhere else the rule is unchanged.
"""
from __future__ import annotations

import pytest

from tools.action_item_kinds import LiveSources, SchemaError, build_envelope

HOSTS = {"github.com", "linear.app"}
CARD = "t_f9e58900"
ENV = {"title": "Give Hermes Full Disk Access", "summary": "The Messages reader needs Full Disk Access.",
       "ask": "Add the Hermes Python to Full Disk Access?", "why": "The Messages reader cannot read chat.db.",
       "risk": {"level": "low", "note": "Grants one local binary disk access."}}
FDA_STEP = ("In the file picker open `.hermes`, then `tools`, then `python-3.14.9+20261101-aarch64-apple-darwin`, "
            "then `bin`, and choose `python3.14`")
PATH = "~/.hermes/tools/python-3.14.9+20261101-aarch64-apple-darwin/bin/python3.14"
REWRITTEN = ["Open System Settings > Privacy & Security > Full Disk Access and press +",
             f"Copy this path: `{PATH}`",
             "In the file picker press Cmd+Shift+G, paste the path and press Enter"]


@pytest.fixture(autouse=True)
def root(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    return tmp_path


def build(kind: str, payload: dict, **overrides) -> dict:
    return build_envelope(kind, payload=payload, sources=LiveSources(), hosts=HOSTS, card=CARD, **{**ENV, **overrides})


def task(steps, where="System Settings on Jack's Mac", **overrides) -> dict:
    return build("user_task", {"steps": steps, "where": where}, **overrides)


def test_the_folder_by_folder_fda_step_is_refused_with_the_field_and_the_fix():
    with pytest.raises(SchemaError) as refused:
        task(["Open System Settings > Privacy & Security > Full Disk Access and press +", FDA_STEP])
    message = str(refused.value)
    assert message.startswith("payload.steps[1]: names a path folder by folder"), message
    assert "Copy this path: `~/.hermes/tools/<dir>/bin/python3.14`" in message
    assert "In the file picker press Cmd+Shift+G, paste the path and press Enter" in message


@pytest.mark.parametrize("step", ["Go to `Users` > `jack` > `.hermes`", "Open `a`, `b`, `c`"])
def test_other_chain_joiners_are_refused(step):
    with pytest.raises(SchemaError, match=r"^payload\.steps\[0\]: names a path folder by folder"):
        task([step])


@pytest.mark.parametrize("step", [
    "Press `Cmd` and `Shift` together",  # two names, joined by "and"
    "Set `REDIS_URL` and `REDIS_TOKEN`, then restart `api`",  # not chained by then/,/> throughout
])
def test_names_that_do_not_walk_a_path_pass(step):
    assert task([step])["payload"]["steps"] == [step]


def test_the_rewritten_steps_with_the_full_path_in_one_code_span_are_accepted():
    assert task(REWRITTEN)["payload"]["steps"] == REWRITTEN


@pytest.mark.parametrize("step", [
    f"Copy this path: {PATH}",  # bare, outside backticks
    f"Copy `{PATH}` into `~/.zshrc`",  # two code spans: the old rule
    f"Copy this path: `python3.14` from {PATH}",  # the path sits outside the one span
])
def test_a_path_outside_a_sole_code_span_in_a_step_is_still_refused(step):
    with pytest.raises(SchemaError, match=r"^payload\.steps\[0\]: contains a local path"):
        task([step])


@pytest.mark.parametrize("kind, payload, overrides, where", [
    ("user_task", {"steps": ["Sign in"], "where": f"`{PATH}`"}, {}, "payload.where"),
    ("user_task", {"steps": ["Sign in"], "where": "Mac"}, {"ask": f"Open `{PATH}`?"}, "ask"),
    ("user_task", {"steps": ["Sign in"], "where": "Mac"}, {"why": f"It is `{PATH}`."}, "why"),
    ("other", {"title": "Rotate", "body": f"The agent rewrites `{PATH}`.", "links": []}, {}, "payload.body"),
])
def test_a_backticked_path_elsewhere_is_still_refused(kind, payload, overrides, where):
    with pytest.raises(SchemaError, match=rf"^{where.replace('.', r'[.]')}: contains a local path"):
        build(kind, payload, **overrides)
