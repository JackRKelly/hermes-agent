"""A user_task that has Jack run SQL carries the SQL itself, never a file he is pointed to.

Jack, Nov 24: a repair task said "Open the repair script at <workspace>/repair-committed-drift.sql". He
reads Action Items from another machine, so neither the path nor the file reaches him. Each statement
goes inline in its step; a script too long for one line goes in one step as a ```sql fenced block, which
the app renders with a Copy button. Also: an item that already shows a local path is listed by
``item_path_problems`` so the readiness check can send it back to its owner.
"""
from __future__ import annotations

import pytest

from tools.action_item_kinds import LiveSources, SchemaError, build_envelope, classify
from tools.action_item_kinds import readability as rd

HOSTS = {"github.com", "linear.app"}
CARD = "t_3a1180b2"
ENV = {"title": "Fix inventory counts after the SQL fix", "summary": "Committed counts drifted on 12 rows.",
       "ask": "Run the repair in prod, then mark done?", "why": "Pick lists show stock that is not there.",
       "risk": {"level": "medium", "note": "Writes 12 prod rows in one transaction."}}
REPAIR = ("Run the repair:\n```sql\nBEGIN;\nUPDATE inventory_level il SET committed = s.committed\n"
          "FROM (SELECT inventory_level_id, sum(quantity) AS committed FROM allocation GROUP BY 1) s\n"
          "WHERE il.id = s.inventory_level_id AND il.committed <> s.committed;\nCOMMIT;\n```")
STEPS = ["Open a prod Postgres session with write access",
         "Preview, expect 12 rows: `SELECT id FROM inventory_level WHERE committed < 0;`",
         REPAIR,
         "Verify, expect 0: `SELECT count(*) FROM inventory_level WHERE committed < 0;`"]


@pytest.fixture(autouse=True)
def root(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    return tmp_path


def task(steps, where="Prod Postgres write session", **overrides) -> dict:
    return build_envelope("user_task", payload={"steps": steps, "where": where}, sources=LiveSources(), hosts=HOSTS,
                          card=CARD, **{**ENV, **overrides})


def test_inline_sql_steps_with_a_fenced_repair_are_accepted_and_read_back():
    env = task(STEPS)
    assert env["payload"]["steps"] == STEPS
    assert classify(env, HOSTS)["fallback"] is None  # the stored item reads, fenced step included


def test_a_long_script_fits_in_one_fenced_step_but_a_long_plain_step_does_not():
    body = "\n".join(f"UPDATE t SET n = {i} WHERE id = {i};" for i in range(40))
    long_block = f"Run:\n```sql\nBEGIN;\n{body}\nCOMMIT;\n```"
    assert rd.STEP_MAX_CHARS < len(long_block) <= rd.STEP_BLOCK_MAX_CHARS
    assert task([long_block])["payload"]["steps"] == [long_block]
    with pytest.raises(SchemaError, match=r"^payload\.steps\[0\]: \d+ chars, max 300; a longer script goes"):
        task([f"Run `{body[:400]}`"])


def test_a_fenced_step_over_the_block_cap_is_refused():
    huge = "Run:\n```sql\n" + "SELECT 1;\n" * 500 + "```"
    with pytest.raises(SchemaError, match=rf"^payload\.steps\[0\]: \d+ chars, max {rd.STEP_BLOCK_MAX_CHARS}"):
        task([huge])


@pytest.mark.parametrize("step, rule", [
    ("Run:\n```sql\nBEGIN;\nCOMMIT;", "never closed"),
    ("See **this** first", "plain text only outside"),
    ("Read [the runbook](https://github.com/x/y) first", "plain text only outside"),
])
def test_a_step_stays_plain_text_outside_a_closed_fence(step, rule):
    with pytest.raises(SchemaError, match=rf"^payload\.steps\[0\]: .*{rule}"):
        task([step])


@pytest.mark.parametrize("step", [
    "Open the repair script `repair-committed-drift.sql` and run it",
    "Run repair-committed-drift.sql in the prod session",
])
def test_a_step_naming_a_sql_file_is_refused(step):
    with pytest.raises(SchemaError) as refused:
        task([step])
    message = str(refused.value)
    assert message.startswith("payload.steps[0]: points Jack to the SQL file 'repair-committed-drift.sql'"), message
    assert "BEGIN;" in message and "COMMIT;" in message


def test_a_sql_file_in_a_github_link_is_a_link_not_a_file():
    step = "Compare with https://github.com/cybership-io/v2/blob/dev/packages/prisma/migrations/x/migration.sql"
    assert task([step])["payload"]["steps"] == [step]


# --- the readiness twin: an item already parked with a path is found -------------------------------

def _stored(steps, **fields) -> dict:
    """An item as stored before this rule: the readiness check sees it, the add-time rules never ran."""
    return {"kind": "user_task", "payload": {"steps": steps, "where": "Prod"}, **ENV, **fields}


def test_item_path_problems_lists_every_field_that_shows_a_path(root):
    script = root / "kanban" / "workspaces" / CARD / "repair-committed-drift.sql"
    item = _stored([f"Open the repair script at `{script}`", "Run `psql -f ~/x.sql`"], why="See /var/log/x.log")
    problems = rd.item_path_problems(item["kind"], item["payload"], title=item["title"], summary=item["summary"],
                                     ask=item["ask"], why=item["why"], risk=item["risk"], card=CARD)
    assert problems == ["why names a local path (/var/log/x.log)",
                        f"payload.steps[0] names a local path ({script})",
                        "payload.steps[1] names a local path (~/x.sql)"]


def test_item_path_problems_is_empty_for_inline_sql_and_an_embedded_image(root):
    image = root / "kanban" / "workspaces" / CARD / "before.png"
    assert rd.item_path_problems("user_task", {"steps": STEPS, "where": "Prod"}, **ENV, card=CARD) == []
    body = f"The drift, before the fix:\nImage: {image}"
    assert rd.item_path_problems("other", {"title": "t", "body": body}, **ENV, card=CARD) == []
