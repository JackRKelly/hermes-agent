"""PR acceptance on repositories with no readable rulesets or no required checks.

Private free-plan repos answer the REST rules endpoint with 403 "Upgrade to GitHub
Pro" even to their owner, so no PR card for them could complete. The gate must then
confirm via GraphQL that no rulesets exist and require every exact-head check to pass
(zero checks only when the repo has no active workflow), while staying closed when
rulesets exist but cannot be read, and on 5xx.
"""
import json
import os
import sys

import pytest

from hermes_cli.kanban_pr_acceptance import collect_acceptance

SHA = "a" * 40
PR = "https://github.com/acme/repo/pull/7"
FREE_PLAN = {"stderr": "gh: Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)",
             "code": 1}


def _run(conclusion="success", name="test", sha=SHA, rid=1):
    return {"id": rid, "name": name, "head_sha": sha, "app": {"id": 15368},
            "status": "completed" if conclusion != "pending" else "in_progress",
            "conclusion": None if conclusion == "pending" else conclusion,
            "html_url": f"https://github.com/acme/repo/runs/{rid}"}


CI = ".github/workflows/ci.yml"
NOT_FOUND = {"stderr": "gh: Not Found (HTTP 404)", "code": 1}


def _routes(*, rules=FREE_PLAN, rulesets=0, protection=None, runs=(), statuses=(),
            workflows=((CI, "active"),), head_files=(CI,)):
    return {
        "graphql": {"stdout": {"data": {"repository": {"pullRequest": {
            "headRefOid": SHA, "baseRefName": "main", "state": "OPEN",
            "baseRef": {"branchProtectionRule": protection}}}}}},
        "graphql_rulesets": {"stdout": {"data": {"repository": {"rulesets": {"totalCount": rulesets}}}}},
        "/rules/branches/": rules,
        "/check-runs": {"stdout": [{"total_count": len(runs), "check_runs": list(runs)}]},
        "/statuses": {"stdout": [list(statuses)]},
        "/actions/workflows": {"stdout": [{"total_count": len(workflows), "workflows": [
            {"id": i, "path": p, "state": s} for i, (p, s) in enumerate(workflows)]}]},
        "/contents/": NOT_FOUND if head_files is None else {"stdout": [
            {"path": p, "type": "file"} for p in head_files]},
        "/pulls/": {"stdout": {"head": {"sha": SHA}, "base": {"ref": "main"}, "state": "open"}},
    }


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """A `gh` on PATH answering from a route table, failing like real gh (stderr + exit 1)."""
    routes_file = tmp_path / "routes.json"
    calls_file = tmp_path / "calls.log"
    shim = tmp_path / "bin"
    shim.mkdir()
    script = shim / "gh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"routes = json.load(open({str(routes_file)!r}))\n"
        "endpoint = sys.argv[2]\n"
        "query = next((a for a in sys.argv if a.startswith('query=')), '')\n"
        f"open({str(calls_file)!r}, 'a').write(endpoint + '\\n')\n"
        "if endpoint == 'graphql':\n"
        "    key = 'graphql_rulesets' if 'rulesets' in query else 'graphql'\n"
        "else:\n"
        "    key = next(k for k in routes if k.startswith('/') and k in endpoint)\n"
        "route = routes[key]\n"
        "if 'stdout' not in route:\n"
        "    sys.stderr.write(route['stderr'] + '\\n'); sys.exit(route['code'])\n"
        "print(json.dumps(route['stdout']))\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])

    def configure(**kwargs):
        routes_file.write_text(json.dumps(_routes(**kwargs)))
        return lambda: calls_file.read_text().split() if calls_file.exists() else []
    return configure


@pytest.mark.platforms("posix")
def test_free_plan_repo_without_rulesets_accepts_green_exact_head_checks(gh):
    gh(runs=[_run("success")])
    receipt = collect_acceptance("acme/repo", PR)
    assert receipt["ok"] and receipt["classification"] == "success"
    assert receipt["policy"] == "all-head-checks"
    assert [c["name"] for c in receipt["checks"]] == ["test"]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("conclusion,expected", [
    ("failure", "failure"), ("pending", "pending"), ("cancelled", "infra"), ("skipped", "infra")])
def test_without_required_checks_every_head_check_must_pass(gh, conclusion, expected):
    gh(runs=[_run("success", "lint", rid=1), _run(conclusion, "test", rid=2)])
    receipt = collect_acceptance("acme/repo", PR)
    assert not receipt["ok"] and receipt["classification"] == expected


@pytest.mark.platforms("posix")
def test_without_required_checks_a_failed_legacy_status_or_stale_run_blocks(gh):
    gh(runs=[_run("success")], statuses=[
        {"id": 2, "context": "ext", "state": "failure", "target_url": None},
        {"id": 1, "context": "ext", "state": "success", "target_url": None}])
    assert collect_acceptance("acme/repo", PR)["classification"] == "failure"
    gh(runs=[_run("success", sha="b" * 40)])
    assert collect_acceptance("acme/repo", PR)["classification"] == "stale"


@pytest.mark.platforms("posix")
def test_zero_checks_accepted_only_when_no_enabled_workflow_can_run_at_head(gh):
    other = ".github/workflows/probe.yml"
    accepted = [
        # every workflow disabled (hermes-agent's live shape)
        dict(workflows=((CI, "disabled_manually"), (other, "disabled_fork"))),
        # the only enabled workflow lives on an unrelated branch, not in the head commit
        dict(workflows=((CI, "disabled_manually"), (other, "active"))),
        # no workflow directory at the head at all
        dict(workflows=((other, "active"),), head_files=None),
        dict(workflows=()),
    ]
    for case in accepted:
        calls = gh(runs=[], **case)
        receipt = collect_acceptance("acme/repo", PR)
        assert receipt["ok"], case
        assert "no enabled workflow" in receipt["detail"]
        assert any("/actions/workflows" in c for c in calls())

    refused = [
        dict(workflows=((CI, "disabled_manually"), (other, "active")), head_files=(CI, other)),
        dict(workflows=(("dynamic/github-code-scanning/codeql", "active"),), head_files=None),
    ]
    for case in refused:
        gh(runs=[], **case)
        receipt = collect_acceptance("acme/repo", PR)
        assert not receipt["ok"] and receipt["classification"] == "missing", case
        assert "wait for CI" in receipt["detail"]


@pytest.mark.platforms("posix")
def test_rules_endpoint_200_empty_also_falls_back_to_head_checks(gh):
    """A public or paid repo with no rulesets (rules endpoint answers []) is the same case."""
    gh(rules={"stdout": [[]]}, runs=[], workflows=((CI, "disabled_manually"),))
    assert collect_acceptance("acme/repo", PR)["ok"]


@pytest.mark.platforms("posix")
def test_unreadable_rules_on_a_repo_that_has_rulesets_stays_closed(gh):
    gh(rulesets=1, runs=[_run("success")])
    receipt = collect_acceptance("acme/repo", PR)
    assert not receipt["ok"] and receipt["classification"] == "auth"
    assert "HTTP 403" in receipt["detail"]
    gh(rules=NOT_FOUND, rulesets=2, runs=[_run("success")])
    assert collect_acceptance("acme/repo", PR)["classification"] == "auth"


@pytest.mark.platforms("posix")
def test_rules_endpoint_server_error_fails_closed_as_infra(gh):
    gh(rules={"stderr": "gh: Server Error (HTTP 502)", "code": 1}, runs=[_run("success")])
    receipt = collect_acceptance("acme/repo", PR)
    assert not receipt["ok"] and receipt["classification"] == "infra"


@pytest.mark.platforms("posix")
def test_classic_protection_still_decides_when_rules_endpoint_is_paywalled(gh):
    """Readable classic protection keeps the required-checks policy: an optional failure
    does not veto, a missing required check does."""
    protection = {"requiredStatusChecks": [{"context": "test", "app": None}]}
    gh(protection=protection, runs=[_run("success", "test", rid=1), _run("failure", "optional", rid=2)])
    receipt = collect_acceptance("acme/repo", PR)
    assert receipt["ok"] and "policy" not in receipt
    assert receipt["required"] == [{"context": "test", "app_id": None}]
    gh(protection=protection, runs=[_run("success", "optional", rid=2)])
    assert collect_acceptance("acme/repo", PR)["classification"] == "missing"
