"""Pinned card skills the assignee profile cannot load.

A card's ``skills`` are preloaded by the ASSIGNEE's worker, under that profile's home. A skill that
does not resolve there makes every attempt fail the same way, so it is refused when the card is
created (``kanban_create`` tool, ``hermes kanban create --skill``) and, for a card that got past
that (created before this check, or the skill was removed later), parked once by the worker with
:func:`missing_skills_block_reason` instead of crashing and spending the retry budget.

The check runs in a FRESH process under the assignee's home (:func:`missing_pinned_skills`), not in
the caller's: agents call ``kanban_create`` from inside a managed worker, where every config reader
returns that worker's frozen session config (``agent/safe_worker_policy.py``), so an in-process
lookup would judge ``skills.external_dirs`` and the disabled list by the CREATOR's profile. A fresh
process reads the assignee's own ``config.yaml``, the file its worker's session config is frozen
from, and the tool and the CLI get the same answer.

Project skills (a trusted repo's ``.hermes/skills``) resolve from the directory the worker runs in:
it chdirs into the card's workspace before the preload (``gateway/session_kanban.py``), and its
policy pins that path as the session cwd. The child pins the same path when the card names, at
create, an existing ``dir`` or ``scratch`` directory (kinds the dispatcher runs verbatim). Otherwise
(a fresh scratch dir, any worktree, a project-linked card) the run-time dir is only settled at
dispatch, so the child answers only when the assignee trusts no project dir at all (the project
tier is then empty wherever the worker runs) and else reports "unknown" and the card is created.

Run as ``python -m hermes_cli.kanban_skill_preflight [--workspace <dir>] -- <skill>...``: prints the
missing ones as one JSON line after :data:`_RESULT_MARKER` (``null`` when it cannot tell).
"""
from __future__ import annotations

# As the child (``python -m``) this module is spawned on the bare ``sys.executable``: in a PM install
# that interpreter carries no dependencies until hermes_bootstrap selects the committed environment,
# so it must run before any import that reaches a third-party package (``ruamel`` via the skills
# lookup), as in ``agent/managed_worker.py``. Without it the child died on import and the check
# failed open. Only as ``__main__``: the create paths import this module in-process, where the
# bootstrap's TMPDIR/scratch exports must not fire in a library importer.
if __name__ == "__main__":
    try:
        import hermes_bootstrap  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name != "hermes_bootstrap":
            raise

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# The card-comment tag Captain triages on: installing a skill into a crew profile is Captain's call.
SKILLS_BLOCK_TAG = "[captain:skills]"

# Prefix of the child's answer line, so a stray print from an import cannot be mistaken for it.
_RESULT_MARKER = "kanban-skill-preflight:"

# Platform env the kanban worker does not bind (``gateway/session_kanban.py`` sets no session
# platform, and nothing exports ``HERMES_PLATFORM``), dropped so the creator's platform cannot pick
# ``skills.platform_disabled`` entries the worker would not apply.
_UNBOUND_PLATFORM_ENV = ("HERMES_PLATFORM", "HERMES_SESSION_PLATFORM")

# Bound on the child (a cold import plus one skills scan, measured at 0.1 to 0.14 s). Past it the
# check does not refuse: the card is created and the run-time park reports a missing skill instead,
# so a hung child holds ``kanban_create`` for seconds, not a minute.
_CHILD_TIMEOUT_S = 10

# Child flag naming the card's workspace, the directory its worker will resolve project skills from.
_WORKSPACE_FLAG = "--workspace"


def missing_skills_block_reason(missing: Iterable[str], profile: str, *, safe_mode: bool = False) -> str:
    """The park reason for a card whose pinned *missing* skills do not load for *profile*.
    *safe_mode*: the worker runs in safe mode, which loads no skills at all, so they are not
    "not installed" there."""
    names = ", ".join(missing)
    if safe_mode:
        return (f"{SKILLS_BLOCK_TAG} pinned skill(s) {names} not loaded: {profile}'s worker runs in "
                "safe mode, which loads no skills")
    return f"{SKILLS_BLOCK_TAG} pinned skill(s) {names} not installed for {profile}"


# Workspace kinds whose worker runs in the given path verbatim (``resolve_workspace``). A
# ``worktree`` path is not one: a repo root anchors ``<repo>/.worktrees/<id>`` and a checkout of
# another branch falls back to the task's own worktree, so its run-time dir is only known at dispatch.
_VERBATIM_WORKSPACE_KINDS = frozenset({"dir", "scratch"})


def _worker_cwd_at_create(kind: str | None, path: str | None) -> str | None:
    """The directory the card's worker will run in, when it is already known at create: an
    absolute existing *path* of a kind the dispatcher uses verbatim. Else None."""
    if kind not in _VERBATIM_WORKSPACE_KINDS or not path:
        return None
    resolved = Path(path).expanduser()
    return str(resolved) if resolved.is_absolute() and resolved.is_dir() else None


def _resolve_in_child(home: str, names: list[str], workspace: str | None = None) -> list[str] | None:
    """Missing *names* as a fresh process under *home* resolves them (project skills from
    *workspace*), or None when the child failed or could not tell."""
    from tools.environments.local import served_profile_child_env
    env = served_profile_child_env(target_home=home, inherit_credentials=False)
    for key in _UNBOUND_PLATFORM_ENV:
        env.pop(key, None)
    # Same interpreter and working directory the managed worker is spawned with
    # (gateway/session_managed_worker.py), so the child imports this checkout's code; like the
    # worker, the child's entry imports hermes_bootstrap first to get its dependencies.
    source_root = Path(__file__).resolve().parents[1]
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "hermes_cli.kanban_skill_preflight",
             *((_WORKSPACE_FLAG, workspace) if workspace else ()), "--", *names],
            cwd=source_root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=_CHILD_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("pinned-skill preflight under %s did not run: %s", home, exc)
        return None
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(_RESULT_MARKER):
            try:
                missing = json.loads(line[len(_RESULT_MARKER):])
            except ValueError:
                break
            if missing is None:  # the child could not tell (see the module docstring)
                return None
            if isinstance(missing, list) and all(isinstance(n, str) for n in missing):
                return missing
            break
    logger.warning("pinned-skill preflight under %s gave no answer (exit %s): %s",
                   home, proc.returncode, (proc.stderr or "").strip()[-500:])
    return None


def missing_pinned_skills(assignee: str | None, skills: Iterable[str] | None,
                          workspace_kind: str | None = None, workspace_path: str | None = None) -> list[str]:
    """Pinned *skills* that would not load for *assignee*'s worker: the lookup its preload runs
    (``build_preloaded_skills_prompt``: the profile's skills dir, its ``skills.external_dirs``,
    the trusted project skills of the card's workspace, operator-disabled skills count as
    missing), done in a fresh process under that profile's home. *workspace_kind* and
    *workspace_path* are the card's workspace as given at create.

    Empty when every skill resolves, and also (never a refusal) when the answer is unknown: no
    assignee or an unknown profile (the dispatcher already refuses to spawn those, and this check
    must not invent a second answer), a workspace not known at create while the assignee trusts
    some project dir, or a child that failed or timed out (logged; the run-time park covers it).
    Plugin-qualified names (``plugin:skill``) are skipped: they resolve through the plugins loaded
    in the worker's process.
    """
    names = list(dict.fromkeys(
        s.strip() for s in skills or () if isinstance(s, str) and s.strip() and ":" not in s))
    if not names or not assignee:
        return []
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        home = resolve_profile_env(normalize_profile_name(assignee))
    except (FileNotFoundError, ValueError):
        return []
    return _resolve_in_child(str(home), names, _worker_cwd_at_create(workspace_kind, workspace_path)) or []


def pinned_skills_error(assignee: str | None, skills: Iterable[str] | None,
                        workspace_kind: str | None = None, workspace_path: str | None = None) -> str | None:
    """The create-time refusal for a card pinning skills its assignee lacks, else None (the
    workspace arguments as :func:`missing_pinned_skills` takes them)."""
    missing = missing_pinned_skills(assignee, skills, workspace_kind, workspace_path)
    if not missing:
        return None
    return (f"pinned skill(s) {', '.join(missing)} not installed for profile {assignee}: its worker "
            "could not load them and every run would fail. Install them into that profile (or "
            "its skills.external_dirs), assign a profile that has them, or drop them from skills.")


def main(argv: list[str]) -> int:
    """Child entry: print which skills (``[--workspace <dir>] -- <skill>...``) this process's home
    cannot load from that workspace, or ``null`` when the missing workspace leaves it unknown."""
    workspace = None
    if argv[:1] == [_WORKSPACE_FLAG]:
        workspace, argv = argv[1], argv[2:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    from agent.skill_commands import unresolved_preloaded_skills
    from agent.skill_utils import project_skills_may_load
    if workspace:
        # The worker's own cwd ladder (``resolve_agent_cwd``) starts at the session cwd its
        # policy pins to the workspace; pinning it here resolves project skills from the same dir.
        from agent.runtime_cwd import set_session_cwd
        set_session_cwd(workspace)
    elif project_skills_may_load():
        # No workspace yet: a trusted project could still supply a skill at run time.
        print(_RESULT_MARKER + "null", flush=True)
        return 0
    print(_RESULT_MARKER + json.dumps(unresolved_preloaded_skills(argv)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
