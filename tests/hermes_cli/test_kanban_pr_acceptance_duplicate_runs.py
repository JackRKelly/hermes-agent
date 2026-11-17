"""PR acceptance when a duplicate workflow run reports the same checks at the head.

A second run of the same workflow at one head (seen on cybership-io/v2 PR 4483:
`lint`, `build` and `PR Preflight` passed in one check suite and concluded
`skipped` in a twin suite) makes GitHub's ``filter=latest`` return both copies. The
skipped twin was classified ``infra`` and refused completion although every
required check passed. A skipped/neutral twin of a passing check must not block; a
failure, or a skipped check with no passing sibling, still does.
"""
import pytest

from hermes_cli.kanban_pr_acceptance import collect_acceptance
from tests.hermes_cli.test_kanban_pr_acceptance_no_rulesets import PR, _run, gh  # noqa: F401  (gh is a fixture)

REQUIRED = {"requiredStatusChecks": [{"context": "lint", "app": {"databaseId": 15368}},
                                     {"context": "build", "app": {"databaseId": 15368}}]}


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("twin", ["skipped", "neutral"])
def test_skipped_duplicate_of_a_passing_required_check_does_not_block(gh, twin):
    gh(protection=REQUIRED, runs=[_run("success", "lint", rid=1), _run("success", "build", rid=2),
                                  _run(twin, "lint", rid=3), _run(twin, "build", rid=4)])
    receipt = collect_acceptance("acme/repo", PR)
    assert receipt["ok"] and receipt["classification"] == "success"
    by_id = {c["id"]: c["classification"] for c in receipt["checks"]}
    assert by_id == {1: "success", 2: "success", 3: "superseded", 4: "superseded"}


@pytest.mark.platforms("posix")
def test_failed_required_check_still_blocks_beside_a_passing_duplicate(gh):
    gh(protection=REQUIRED, runs=[_run("success", "lint", rid=1), _run("failure", "lint", rid=2),
                                  _run("success", "build", rid=3)])
    receipt = collect_acceptance("acme/repo", PR)
    assert not receipt["ok"] and receipt["classification"] == "failure"


@pytest.mark.platforms("posix")
def test_skipped_required_check_without_a_passing_sibling_still_blocks(gh):
    gh(protection=REQUIRED, runs=[_run("skipped", "lint", rid=1), _run("success", "build", rid=2)])
    receipt = collect_acceptance("acme/repo", PR)
    assert not receipt["ok"] and receipt["classification"] == "infra"


@pytest.mark.platforms("posix")
def test_without_required_checks_a_skipped_duplicate_of_a_passing_check_does_not_block(gh):
    gh(runs=[_run("success", "test", rid=1), _run("skipped", "test", rid=2)])
    receipt = collect_acceptance("acme/repo", PR)
    assert receipt["ok"] and receipt["policy"] == "all-head-checks"
