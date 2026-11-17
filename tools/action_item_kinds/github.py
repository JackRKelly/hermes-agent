"""The GitHub reader behind ``pr_merge``'s ``enrich`` and ``live_check``: the host's ``gh`` login.

Read-only (``gh api`` GETs and ``gh pr view``); nothing here merges. A failed or missing ``gh`` is
``SourceUnavailable`` so callers fail closed.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Optional

from tools.action_item_kinds.spec import LiveSources, SourceUnavailable

GH_TIMEOUT_SECONDS = 30
_VIEW_FIELDS = "reviewDecision,mergeStateStatus,mergeable,statusCheckRollup"
_GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_PENDING = {"PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", "REQUESTED", ""}


def _gh_json(args: list[str]) -> Any:
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=GH_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceUnavailable(f"gh {' '.join(args[:2])} failed: {exc}") from exc
    if proc.returncode != 0:
        raise SourceUnavailable(f"gh {' '.join(args[:2])} exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise SourceUnavailable(f"gh {' '.join(args[:2])} returned non-JSON output") from exc


def _check_state(entry: dict) -> str:
    """``green`` / ``pending`` / ``red`` for one ``statusCheckRollup`` entry."""
    if entry.get("__typename") == "StatusContext" or "context" in entry:
        state = str(entry.get("state") or "").upper()
        return "green" if state == "SUCCESS" else "pending" if state in _PENDING else "red"
    if str(entry.get("status") or "").upper() != "COMPLETED":
        return "pending"
    return "green" if str(entry.get("conclusion") or "").upper() in _GREEN else "red"


def summarize_checks(rollup: Optional[list]) -> dict:
    states = [_check_state(e) for e in rollup or [] if isinstance(e, dict)]
    return {"total": len(states), "green": states.count("green"), "pending": states.count("pending"),
            "red": states.count("red")}


class GhSources(LiveSources):
    def github_pr(self, repo: str, number: int) -> dict:
        pull = _gh_json(["api", f"repos/{repo}/pulls/{int(number)}"])
        view = _gh_json(["pr", "view", str(int(number)), "--repo", repo, "--json", _VIEW_FIELDS])
        head, base = pull.get("head") or {}, pull.get("base") or {}
        head_repo = (head.get("repo") or {}).get("full_name")
        return {
            "state": "MERGED" if pull.get("merged") else str(pull.get("state") or "").upper(),
            "title": pull.get("title") or "", "body": pull.get("body") or "",
            "head_sha": head.get("sha") or "", "head_ref": head.get("ref") or "",
            "base_ref": base.get("ref") or "", "cross_repo": head_repo not in (None, repo),
            "commits": int(pull.get("commits") or 0), "files": int(pull.get("changed_files") or 0),
            "mergeable": view.get("mergeable") or "", "merge_state": view.get("mergeStateStatus") or "",
            "review_decision": view.get("reviewDecision") or "",
            "checks": summarize_checks(view.get("statusCheckRollup")),
        }

    def github_merge_method(self, repo: str) -> str:
        info = _gh_json(["api", f"repos/{repo}"])
        allowed = [m for m, key in (("squash", "allow_squash_merge"), ("merge", "allow_merge_commit"),
                                    ("rebase", "allow_rebase_merge")) if info.get(key)]
        if not allowed:
            raise SourceUnavailable(f"{repo} allows no merge method")
        return allowed[0]
