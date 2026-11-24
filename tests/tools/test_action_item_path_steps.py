"""A user_task step never names a local path: not whole, not in its one code span, not folder by folder.

Jack, Nov 14: a Full Disk Access task named its path folder by folder ("Open `.hermes`, then `tools`,
then ..., then `bin`") and he could not follow it. The fix then let a step carry the full ``~/`` path as its
only code span. Jack, Nov 24: a repair task's step said "Open the repair script at
~/.hermes/kanban/workspaces/<card>/repair-committed-drift.sql", and he reads Action Items on another
machine, so that path (and that file) is useless to him. A step now carries the SQL or command itself.
"""
from __future__ import annotations

import pytest

from tools.action_item_kinds import LiveSources, SchemaError, build_envelope

HOSTS = {"github.com", "linear.app"}
CARD = "t_3a1180b2"
ENV = {"title": "Fix inventory counts after the SQL fix", "summary": "Committed counts drifted on 12 rows.",
       "ask": "Run the repair in prod, then mark done?", "why": "Pick lists show stock that is not there.",
       "risk": {"level": "medium", "note": "Writes 12 prod rows in one transaction."}}
FDA_STEP = ("In the file picker open `.hermes`, then `tools`, then `python-3.14.9+202****1101-aarch64-apple-darwin`, "
            "then `bin`, and choose `python3.14`")
PATH = "~/.hermes/tools/python-3.14.9+202****1101-aarch64-apple-darwin/bin/python3.14"


@pytest.fixture(autouse=True)
def root(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    return tmp_path


def build(kind: str, payload: dict, **overrides) -> dict:
    return build_envelope(kind, payload=payload, sources=LiveSources(), hosts=HOSTS, card=CARD, **{**ENV, **overrides})


def task(steps, where="Prod Postgres write session", **overrides) -> dict:
    return build("user_task", {"steps": steps, "where": where}, **overrides)


def test_a_workspace_file_in_backticks_is_refused_with_inline_sql_as_the_fix(root):
    """In the card's own workspace and a type the app serves, but in `code`: the app prints it as text."""
    script = root / "kanban" / "workspaces" / CARD / "repair-committed-drift.md"
    with pytest.raises(SchemaError) as refused:
        task([f"Open the repair notes at `{script}`", "Run it"])
    message = str(refused.value)
    assert message.startswith("payload.steps[0]: contains a local path"), message
    assert "another machine" in message and "```sql" in message
    assert "SELECT" in message and "BEGIN;" in message  # the example is the SQL itself


@pytest.mark.parametrize("step", [
    f"Copy this path: `{PATH}`",  # the old sole-code-span exception
    "In the file picker press Cmd+Shift+G, paste `~/.hermes/bin` and press Enter",
    f"Copy this path: {PATH}",
    "Run `ls $HOME/backups`",
    "Run `cat ${HERMES_HOME}/logs/agent.log`",
    "Open `/private/var/folders/br/x/out.txt`",
])
def test_a_local_path_in_a_step_is_refused_even_as_its_only_code_span(step):
    with pytest.raises(SchemaError, match=r"^payload\.steps\[0\]: contains a local path"):
        task([step])


def test_the_folder_by_folder_step_is_still_refused():
    with pytest.raises(SchemaError, match=r"^payload\.steps\[1\]: names a local path folder by folder"):
        task(["Open System Settings > Privacy & Security > Full Disk Access and press +", FDA_STEP])


@pytest.mark.parametrize("step", ["Go to `Users` > `jack` > `.hermes`", "Open `a`, `b`, `c`"])
def test_other_chain_joiners_are_refused(step):
    with pytest.raises(SchemaError, match=r"^payload\.steps\[0\]: names a local path folder by folder"):
        task([step])


@pytest.mark.parametrize("step", [
    "Press `Cmd` and `Shift` together",  # two names, joined by "and"
    "Set `REDIS_URL` and `REDIS_TOKEN`, then restart `api`",  # not chained by then/,/> throughout
    "Run `echo $HOMEBREW_PREFIX`",  # another variable that only starts with HOME
])
def test_names_and_commands_without_a_path_pass(step):
    assert task([step])["payload"]["steps"] == [step]


@pytest.mark.parametrize("kind, payload, overrides, where", [
    ("user_task", {"steps": ["Sign in"], "where": f"`{PATH}`"}, {}, "payload.where"),
    ("user_task", {"steps": ["Sign in"], "where": "Mac"}, {"ask": f"Open `{PATH}`?"}, "ask"),
    ("user_task", {"steps": ["Sign in"], "where": "Mac"}, {"why": f"It is `{PATH}`."}, "why"),
    ("user_task", {"steps": ["Sign in"], "where": "Mac"}, {"summary": "The log is in /var/log/x.log now."}, "summary"),
    ("other", {"title": "Rotate", "body": f"The agent rewrites `{PATH}`.", "links": []}, {}, "payload.body"),
])
def test_a_path_in_any_other_text_is_refused(kind, payload, overrides, where):
    with pytest.raises(SchemaError, match=rf"^{where.replace('.', r'[.]')}: contains a local path"):
        build(kind, payload, **overrides)
