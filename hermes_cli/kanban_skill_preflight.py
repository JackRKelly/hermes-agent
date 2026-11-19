"""Pinned card skills the assignee profile cannot load.

A card's ``skills`` are preloaded by the ASSIGNEE's worker, under that profile's home. A skill that
does not resolve there makes every attempt fail the same way, so it is refused when the card is
created (``kanban_create`` tool, ``hermes kanban create --skill``) and, for a card that got past
that (created before this check, or the skill was removed later), parked once by the worker with
:func:`missing_skills_block_reason` instead of crashing and spending the retry budget.
"""
from __future__ import annotations

from typing import Iterable

# The card-comment tag Captain triages on: installing a skill into a crew profile is Captain's call.
SKILLS_BLOCK_TAG = "[captain:skills]"


def missing_skills_block_reason(missing: Iterable[str], profile: str) -> str:
    """The park reason for a card whose pinned *missing* skills do not load for *profile*."""
    return f"{SKILLS_BLOCK_TAG} pinned skill(s) {', '.join(missing)} not installed for {profile}"


def missing_pinned_skills(assignee: str | None, skills: Iterable[str] | None) -> list[str]:
    """Pinned *skills* that would not load for *assignee*'s worker: the resolution its preload
    runs (``build_preloaded_skills_prompt``: the profile's skills dir, its ``skills.external_dirs``,
    operator-disabled skills count as missing), done under that profile's home.

    Empty when every skill resolves, and when the assignee's home cannot be resolved (no assignee,
    unknown profile): the dispatcher already refuses to spawn those, and this check must not invent
    a second answer. Plugin-qualified names (``plugin:skill``) are skipped: they resolve through the
    plugins loaded in the WORKER's process, which this process cannot see, so the run-time park
    covers them.
    """
    names = [s.strip() for s in skills or () if isinstance(s, str) and s.strip() and ":" not in s]
    if not names or not assignee:
        return []
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        home = resolve_profile_env(normalize_profile_name(assignee))
    except (FileNotFoundError, ValueError):
        return []
    from agent.skill_commands import unresolved_preloaded_skills
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    # Context-local, never os.environ: the skills dir, config (external_dirs, disabled list) and the
    # skill scan all read get_hermes_home() at call time.
    token = set_hermes_home_override(home)
    try:
        return unresolved_preloaded_skills(names)
    finally:
        reset_hermes_home_override(token)


def pinned_skills_error(assignee: str | None, skills: Iterable[str] | None) -> str | None:
    """The create-time refusal for a card pinning skills its assignee lacks, else None."""
    missing = missing_pinned_skills(assignee, skills)
    if not missing:
        return None
    return (f"pinned skill(s) {', '.join(missing)} not installed for profile {assignee}: its worker "
            "could not load them and every run would fail. Install them into that profile (or "
            "its skills.external_dirs), assign a profile that has them, or drop them from skills.")
