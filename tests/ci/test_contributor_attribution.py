"""Execute the attribution workflow against real, divergent Git histories."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import hermes_yaml as yaml
import pytest

from tests.ci import _gha_expr as gha

REPO = Path(__file__).resolve().parents[2]
MAPPED = "mapped@example.invalid"
HISTORICAL = "historical@example.invalid"
NEW = "new@example.invalid"


def git(repo, *args, email=MAPPED):
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Fixture Author",
        GIT_AUTHOR_EMAIL=email,
        GIT_COMMITTER_NAME="Fixture Committer",
        GIT_COMMITTER_EMAIL=email,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    return subprocess.check_output(
        ["git", *args],
        cwd=repo,
        env=env,
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "contributors/emails").mkdir(parents=True)
    (repo / "contributors/emails" / MAPPED).write_text("fixture-user\n")
    (repo / "scripts/releases").mkdir(parents=True)
    (repo / "scripts/releases/authors_legacy.py").write_text("AUTHOR_MAP = {}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial mapped history")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(
        repo,
        "commit",
        "--allow-empty",
        "-m",
        "unrelated upstream work",
        email=HISTORICAL,
    )
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "custom-base")
    git(repo, "switch", "-c", "pr")
    git(repo, "commit", "--allow-empty", "-m", "mapped PR work")
    return repo, base


def workflow(repo, base=None, event="pull_request"):
    doc = yaml.safe_load((REPO / ".github/workflows/contributor-check.yml").read_text())
    step = next(
        s
        for s in doc["jobs"]["check-attribution"]["steps"]
        if s.get("id") == "check-emails"
    )
    context = {"github": {"event": {"pull_request": {"base": {"sha": base}}}}}
    env = dict(os.environ, GITHUB_EVENT_NAME=event, GITHUB_OUTPUT=str(repo / "output"))
    env.update(
        {
            key: gha.to_string(gha.render(value, context))
            for key, value in step.get("env", {}).items()
        }
    )
    bash = shutil.which("bash")
    assert bash is not None
    assert step.get("shell") == "bash"
    # Execute the exact checked-in run block, including all mapping checks and jq.
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_workflow_uses_actual_custom_pr_base(history):
    repo, base = history
    result = workflow(repo, base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All contributor emails are mapped" in result.stdout
    assert (repo / "output").read_text() == "review_status=[]\n"
    assert (repo / "review-status.json").read_text() == "review_status=[]\n"


@pytest.mark.parametrize("event", ["pull_request", "pull_request_target"])
def test_workflow_still_rejects_new_unmapped_pr_author(history, event):
    repo, base = history
    git(repo, "commit", "--allow-empty", "-m", "new contributor", email=NEW)
    result = workflow(repo, base, event)
    assert result.returncode == 1, result.stdout + result.stderr
    status = json.loads((repo / "output").read_text().removeprefix("review_status="))
    finding = status[0]["results"][0]
    assert finding["kind"] == "action_required"
    assert NEW in finding["detail"]
    assert HISTORICAL not in finding["detail"]
    assert (repo / "review-status.json").read_text() == (repo / "output").read_text()


@pytest.mark.parametrize(
    "base", [None, "", "HEAD", "missing-ref", "0" * 40, "--all", "HEAD; touch injected"]
)
def test_workflow_fails_closed_for_missing_or_invalid_pr_base(history, base):
    repo, _ = history
    git(repo, "commit", "--allow-empty", "-m", "new contributor", email=NEW)
    result = workflow(repo, base)
    assert result.returncode != 0
    assert not (repo / "output").exists()
    assert not (repo / "review-status.json").exists()
    assert not (repo / "injected").exists()


def test_workflow_keeps_origin_main_for_non_pr(history):
    repo, base = history
    result = workflow(repo, base, event="push")
    assert result.returncode == 1
    assert HISTORICAL in result.stdout


def test_workflow_handles_no_new_commits(history):
    repo, base = history
    git(repo, "reset", "--hard", base)
    result = workflow(repo, base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No new commits to check" in result.stdout


def test_workflow_uses_merge_base_when_target_has_advanced(history):
    repo, _ = history
    git(repo, "switch", "custom-base")
    git(repo, "commit", "--allow-empty", "-m", "target advances", email=HISTORICAL)
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "pr")
    git(repo, "commit", "--allow-empty", "-m", "new contributor", email=NEW)
    result = workflow(repo, base)
    assert result.returncode == 1
    assert NEW in result.stdout
    assert HISTORICAL not in result.stdout


@pytest.mark.parametrize(
    "email",
    [
        MAPPED,
        "legacy@example.invalid",
        "42+fixture@users.noreply.github.com",
        "dependabot@example.invalid",
        "fixture@anthropic.com",
    ],
)
def test_workflow_preserves_existing_mapping_paths(history, email):
    repo, base = history
    (repo / "scripts/releases/authors_legacy.py").write_text(
        'AUTHOR_MAP = {"legacy@example.invalid": "fixture"}\n'
    )
    git(
        repo,
        "commit",
        "--allow-empty",
        "-m",
        "existing mapped contributor",
        email=email,
    )
    result = workflow(repo, base)
    assert result.returncode == 0, result.stdout + result.stderr


def audit(repo, *args):
    script = repo / "scripts/audit_pr_attribution.py"
    shutil.copyfile(REPO / "scripts/audit_pr_attribution.py", script)
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def test_local_audit_accepts_custom_base(history):
    repo, base = history
    result = audit(repo, "--base-ref", base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All contributor emails" in result.stdout


def test_local_audit_still_rejects_new_unmapped_author(history):
    repo, base = history
    git(repo, "commit", "--allow-empty", "-m", "new contributor", email=NEW)
    result = audit(repo, "--base-ref", base)
    assert result.returncode == 1
    assert NEW in result.stdout
    assert HISTORICAL not in result.stdout
    assert not (repo / "contributors/emails" / NEW).exists()


def test_local_audit_defaults_to_origin_main(history):
    repo, _ = history
    result = audit(repo)
    assert result.returncode == 1
    assert HISTORICAL in result.stdout


@pytest.mark.parametrize(
    "base", ["", "missing-ref", "0" * 40, "--all", "HEAD; touch injected"]
)
def test_local_audit_fails_closed_for_invalid_base(history, base):
    repo, _ = history
    git(repo, "commit", "--allow-empty", "-m", "new contributor", email=NEW)
    result = audit(repo, f"--base-ref={base}")
    assert result.returncode != 0
    assert "All contributor emails" not in result.stdout
    assert not (repo / "injected").exists()


def test_workflow_fails_closed_for_unrelated_history(history):
    repo, _ = history
    git(repo, "switch", "--orphan", "unrelated")
    git(repo, "commit", "--allow-empty", "-m", "unrelated root")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "pr")
    result = workflow(repo, base)
    assert result.returncode != 0
    assert not (repo / "output").exists()


def test_workflow_checks_pr_authors_at_synthetic_merge_head(history):
    repo, _ = history
    git(repo, "commit", "--allow-empty", "-m", "new contributor", email=NEW)
    git(repo, "switch", "custom-base")
    git(repo, "commit", "--allow-empty", "-m", "target advances", email=HISTORICAL)
    base = git(repo, "rev-parse", "HEAD")
    git(
        repo,
        "merge",
        "--no-ff",
        "pr",
        "-m",
        "synthetic PR merge",
        email="merge@example.invalid",
    )
    result = workflow(repo, base)
    assert result.returncode == 1
    assert NEW in result.stdout
    assert HISTORICAL not in result.stdout
    assert "merge@example.invalid" not in result.stdout
