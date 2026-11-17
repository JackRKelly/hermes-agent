"""A card workspace that is itself a hermes-agent checkout must not shadow the install.

The dispatcher spawns its worker with ``cwd`` = the card workspace. A plain ``python -m`` puts
that directory first on ``sys.path``, so a workspace that is a hermes-agent worktree supplied
its own ``hermes_bootstrap`` / ``hermes_cli``: the worktree's bootstrap resolved its root to the
worktree and refused to start ("no dependency environment is committed for this install"),
crashing every crew card whose workspace was a hermes-agent worktree. The spawned argv must
import Hermes from the install that spawned it, never from the workspace.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_db import Task

REPO_ROOT = Path(dispatch.__file__).resolve().parents[1]
DECOY_EXIT = 97


def _task() -> Task:
    return Task(id="t_safe1", title="t", body="", assignee="worker", status="running", priority=0,
                created_by=None, created_at=1, started_at=None, completed_at=None,
                workspace_kind="dir", workspace_path=None, claim_lock=None, claim_expires=None,
                tenant=None)


def _decoy_workspace(root: Path, marker: Path) -> Path:
    """A workspace laid out like a hermes-agent checkout whose Hermes modules record their import."""
    stub = (f"import pathlib, sys\npathlib.Path({str(marker)!r}).write_text(__name__)\n"
            f"sys.exit({DECOY_EXIT})\n")
    (root / "hermes_cli").mkdir(parents=True)
    (root / "hermes_bootstrap.py").write_text(stub)
    (root / "hermes_cli" / "__init__.py").write_text(stub)
    return root


@pytest.mark.parametrize("argv_for, extra", [
    (lambda: dispatch._worker_argv(_task(), "worker", None), []),
    (dispatch._module_hermes_argv, ["--version"]),
], ids=["worker_argv", "module_hermes_argv"])
def test_spawned_argv_never_imports_hermes_from_the_card_workspace(tmp_path, argv_for, extra):
    marker = tmp_path / "decoy-imported"
    workspace = _decoy_workspace(tmp_path / "workspace", marker)
    # The dispatcher pins the install root on the worker's PYTHONPATH; no claim env is given, so
    # the real worker client exits at once after its imports.
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN_")}
    env.update(PYTHONPATH=str(REPO_ROOT), HERMES_HOME=str(tmp_path / "home"),
               HTTPS_PROXY="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9")
    r = subprocess.run([*argv_for(), *extra], cwd=workspace, env=env, capture_output=True,
                       text=True, timeout=60, check=False)
    assert not marker.exists(), f"workspace {marker.read_text()} shadowed the install"
    assert r.returncode != DECOY_EXIT, r.stderr[-400:]
