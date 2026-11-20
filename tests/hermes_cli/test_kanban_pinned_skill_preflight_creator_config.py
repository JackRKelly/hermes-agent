"""The pinned-skill preflight judges the ASSIGNEE's config even when the creator is a managed worker.

Agents create cards from inside a managed worker, where every config reader returns that worker's
frozen session config. An in-process check therefore read ``skills.external_dirs`` and the disabled
list from the CREATOR: a creator whose config lists the shared skill root accepted a card for an
assignee without it (the RCA case: a reviewer card pinning a skill only the shared root carries),
and a creator without that root refused a valid card for an assignee that has it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Runs in a fresh interpreter: the worker policy binds once, before any runtime import.
_CREATOR = """
import json, sys
from agent.safe_worker_policy import _bind_safe_worker_policy, _bind_worker_config
creator_config, safe_mode = json.loads(sys.argv[1]), sys.argv[2] == "safe"
if safe_mode:
    _bind_safe_worker_policy(safe_mode=True, ignore_user_config=True, config=creator_config)
else:
    _bind_worker_config(creator_config)
from hermes_cli.kanban_skill_preflight import missing_pinned_skills
print("RESULT " + json.dumps({p: missing_pinned_skills(p, ["shared-only"]) for p in ("has-shared", "no-shared")}))
"""


def _write_skill(root: Path, name: str) -> None:
    skill_dir = root / "testing" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: fixture skill {name}\n---\n\n# {name}\n", encoding="utf-8")


@pytest.fixture
def layout(tmp_path):
    """Profiles ``has-shared`` (external_dirs -> the shared root) and ``no-shared``; the skill
    lives only in the shared root."""
    default = tmp_path / ".hermes"
    shared = tmp_path / "shared-skills"
    _write_skill(shared, "shared-only")
    for name, config in (("has-shared", f"skills:\n  external_dirs:\n    - {shared}\n"),
                         ("no-shared", "model: {}\n")):
        home = default / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(config, encoding="utf-8")
    return tmp_path, default, shared


def _run_creator(tmp_path: Path, default: Path, creator_config: dict, mode: str = "worker") -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "PYTHONPATH"))}
    env.update(HOME=str(tmp_path), HERMES_HOME=str(default))
    proc = subprocess.run([sys.executable, "-c", _CREATOR, json.dumps(creator_config), mode],
                          cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120, check=False)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, proc.stderr[-2000:]
    return json.loads(lines[-1][len("RESULT "):])


def test_creator_with_shared_root_does_not_accept_for_an_assignee_without_it(layout):
    tmp_path, default, shared = layout
    result = _run_creator(tmp_path, default, {"skills": {"external_dirs": [str(shared)]}})
    assert result == {"has-shared": [], "no-shared": ["shared-only"]}


def test_creator_without_shared_root_does_not_refuse_for_an_assignee_with_it(layout):
    tmp_path, default, _shared = layout
    result = _run_creator(tmp_path, default, {"skills": {"disabled": ["shared-only"]}})
    assert result == {"has-shared": [], "no-shared": ["shared-only"]}


def test_safe_mode_creator_still_judges_the_assignee(layout):
    """A safe-mode creator loads no skills itself; the assignee's worker does."""
    tmp_path, default, _shared = layout
    assert _run_creator(tmp_path, default, {}, mode="safe") == {"has-shared": [], "no-shared": ["shared-only"]}


def test_lookup_installs_no_skill_deps(tmp_path, monkeypatch):
    """Answering "would it load?" must not run the skill's activation (``deps`` installs)."""
    import pm
    from agent.skill_commands import unresolved_preloaded_skills
    skill_dir = tmp_path / "skills" / "testing" / "with-deps"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: with-deps\ndescription: d\ndeps: [ffmpeg]\n---\n\n# with-deps\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    installs: list[str] = []  # skill_view swallows an ensure() error, so record calls instead
    monkeypatch.setattr(pm, "ensure", installs.append)

    assert unresolved_preloaded_skills(["with-deps", "absent-skill"]) == ["absent-skill"]
    assert installs == []
