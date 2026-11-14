"""Hermes executable-path helpers for the dispatcher's worker argv (``_resolve_hermes_argv``).

Split out of ``hermes_cli.kanban_db_dispatch``; the resolver and its patch seams
(``_safe_which_no_cwd``, ``_module_hermes_argv``) stay there and are read late-bound.
"""

from __future__ import annotations

import os
import re


def _is_windows() -> bool:
    from hermes_cli import kanban_db
    return kanban_db._IS_WINDOWS


def _absolute_hermes_path(path: str) -> str:
    """Return an absolute filesystem path for a resolved Hermes shim."""
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(expanded)


def _looks_like_path(value: str) -> bool:
    """Return true when a command override is an explicit path, not a name."""
    expanded = os.path.expanduser(value)
    return (
        expanded.startswith("~")
        or os.path.isabs(expanded)
        or bool(os.path.dirname(expanded))
        or "\\" in expanded
        or bool(re.match(r"^[A-Za-z]:", expanded))
    )


def _is_windows_batch_shim(path: str) -> bool:
    """Return true for Windows shell/batch shims that should not be argv[0]."""
    return path.lower().endswith((".cmd", ".bat"))


def _path_search_names(command: str) -> list[str]:
    """Return executable names to try for an unqualified command."""
    if not _is_windows() or os.path.splitext(command)[1]:
        return [command]
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    return [command + ext for ext in raw.split(";") if ext]


def _hermes_path_argv(path: str) -> list[str]:
    """argv for a resolved Hermes executable path. Windows batch shims
    (``.cmd``/``.bat``) are unsafe as argv[0] because the argument vector
    includes task-derived values; prefer the module form."""
    if _is_windows() and _is_windows_batch_shim(path):
        from hermes_cli.kanban_db_dispatch import _module_hermes_argv
        return _module_hermes_argv()
    return [_absolute_hermes_path(path)]
