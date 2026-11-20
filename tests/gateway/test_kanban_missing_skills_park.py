"""A managed Kanban attempt whose pinned skills do not load parks the card once, as itself.

It used to raise ``kanban_skills_unavailable``: the owner settled the admission ``unknown``, the
submitter reported "Kanban execution unknown; retained by owner" and the dispatcher booked a crash
on every attempt, so each unblock spent the retry budget on a failure that could never clear.
"""
import json
import os

from gateway import session_kanban
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing


def _bound_attempt(tmp_path, monkeypatch, skills):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    path = kb.kanban_db_path()
    with connect_closing(path) as conn:
        task_id = kb.create_task(conn, title="reviewer card", assignee="reviewer", skills=skills)
        task = kb.claim_task(conn, task_id, claimer="private-host:owner")
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "worker_bound",
                             {"pid": os.getpid(), "claim_lock": task.claim_lock}, run_id=task.current_run_id)
    context = {"db": str(path), "task_id": task_id, "run_id": task.current_run_id,
               "claim_lock": task.claim_lock, "skills": skills, "profile": "reviewer",
               "context": "", "goal_mode": False}
    return path, context


class _AgentThatMustNotRun:
    session_id = "kanban-session"

    def run_conversation(self, *_args, **_kwargs):
        raise AssertionError("the model ran although no pinned skill loaded")


def test_missing_pinned_skill_parks_the_card_instead_of_crashing(tmp_path, monkeypatch):
    path, context = _bound_attempt(tmp_path, monkeypatch, ["not-installed-review"])
    frame = {"policy": {"kanban_json": json.dumps(context)}, "text": "work kanban task"}

    result = session_kanban.run_worker_turns(_AgentThatMustNotRun(), frame, [])

    reason = "[captain:skills] pinned skill(s) not-installed-review not installed for reviewer"
    assert result["final_response"] == reason
    with connect_closing(path) as conn:
        task = kb.get_task(conn, context["task_id"])
        blocked = [e for e in kb.list_events(conn, task.id) if e.kind == "blocked"]
        run = kb.latest_run(conn, task.id)
    assert (task.status, task.block_kind, task.consecutive_failures) == ("blocked", "capability", 0)
    assert blocked[-1].payload["reason"] == reason
    assert (run.outcome, run.summary) == ("blocked", reason)
    # A settled attempt, not a crash: the submitter reads exit 0 and prints the reason to the log.
    assert session_kanban.worker_exit_code(path, context) == 0
    assert session_kanban.worker_result(path, context)["last_output"] == reason


def test_safe_mode_worker_park_does_not_claim_the_skill_is_missing(tmp_path, monkeypatch):
    """Safe mode loads no skill on purpose: the reason says so instead of "not installed"."""
    import agent.safe_worker_policy as policy
    path, context = _bound_attempt(tmp_path, monkeypatch, ["reviewer-skill"])
    monkeypatch.setattr(policy, "safe_worker_enabled", lambda: True)

    result = session_kanban._park_unavailable_skills(context, ["reviewer-skill"])

    assert result["final_response"] == ("[captain:skills] pinned skill(s) reviewer-skill not loaded: "
                                        "reviewer's worker runs in safe mode, which loads no skills")
    with connect_closing(path) as conn:
        assert kb.get_task(conn, context["task_id"]).block_kind == "capability"


def test_park_is_fenced_to_the_attempt_run(tmp_path, monkeypatch):
    """A reclaimed attempt (its run superseded) must not block the card's newer run."""
    path, context = _bound_attempt(tmp_path, monkeypatch, ["not-installed-review"])
    stale = dict(context, run_id=context["run_id"] + 1000)

    session_kanban._park_unavailable_skills(stale, ["not-installed-review"])

    with connect_closing(path) as conn:
        assert kb.get_task(conn, context["task_id"]).status == "running"
