"""``pr_merge`` v1: merge one PR into its base, on Jack's approval of that exact head (design §3.2).

The agent names ``repo``, ``number`` and ``head_sha`` (and may pin ``method``); ``enrich`` reads
everything else from the live PR and refuses when the PR is not open or its head is not the named
sha, so an agent cannot describe a PR it did not read. ``live_check`` re-reads the PR and reports
drift on every bound field. The §3.2 review condition (crew approval at head, or a promotion's
constituent check) needs the dashboard plugin's mirror and stays the plugin's and the executor's
job: ``live_check`` lists it under ``unverified``.
"""

from __future__ import annotations

import re
from typing import Optional

from tools.action_item_kinds import legacy
from tools.action_item_kinds.core import SchemaError
from tools.action_item_kinds.spec import (
    KindSpec, LiveSources, SourceUnavailable, live_result, live_unavailable, load_versions,
)

KIND = "pr_merge"
AGENT_FIELDS = frozenset({"repo", "number", "head_sha", "method"})
LONG_LIVED = frozenset({"dev", "staging", "main"})
TLDR_MAX = 2000
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_TLDR_TITLE_RE = re.compile(r"^tl;?\s*dr\b", re.I)
_SANITIZE = (
    (re.compile(r"<!--.*?-->", re.S), ""),
    (re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(\s[^<>]*)?/?>"), ""),
    (re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)"), ""),
    (re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)"), r"\1"),
    (re.compile(r"\*\*([^*\n]+)\*\*"), r"\1"),
    (re.compile(r"^\s*```.*$", re.M), ""),
    (re.compile(r"\n{3,}"), "\n\n"),
)


def plain(text: str) -> str:
    """Source markdown/HTML as plain text for display (the PR body is untrusted, §5.4)."""
    for pattern, repl in _SANITIZE:
        text = pattern.sub(repl, text)
    return "\n".join(_HEADING_RE.sub(r"\1", line) for line in text.split("\n")).strip()


def extract_tldr(body: str) -> str:
    """The PR body's ``## TL;DR`` section as plain text (empty when there is none)."""
    lines, out, inside = body.replace("\r\n", "\n").split("\n"), [], False
    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading and line.lstrip().startswith("#"):
            if inside:
                break
            inside = bool(_TLDR_TITLE_RE.match(heading.group(1)))
            continue
        if inside:
            out.append(line)
    text = plain("\n".join(out))
    return text if len(text) <= TLDR_MAX else text[:TLDR_MAX - 1].rstrip() + "\u2026"


def is_promotion(pr: dict) -> bool:
    return not pr["cross_repo"] and pr["head_ref"] in LONG_LIVED and pr["base_ref"] in LONG_LIVED


def needs_admin(pr: dict) -> bool:
    """True only when the sole merge blocker is the required-human-review rule."""
    checks = pr["checks"]
    return (pr["review_decision"] == "REVIEW_REQUIRED" and pr["merge_state"] == "BLOCKED"
            and pr["mergeable"] == "MERGEABLE" and checks["red"] == 0 and checks["pending"] == 0)


def _read_pr(agent: dict, sources: LiveSources) -> dict:
    try:
        return sources.github_pr(agent["repo"], agent["number"])
    except SourceUnavailable as exc:
        raise SchemaError(f"payload: could not read {agent['repo']}#{agent['number']} ({exc})") from exc


def _enrich(agent: dict, sources: LiveSources) -> dict:
    missing = sorted({"repo", "number", "head_sha"} - set(agent))
    if missing:
        raise SchemaError(f"payload: missing keys {missing}")
    if not isinstance(agent["number"], int) or isinstance(agent["number"], bool):
        raise SchemaError("payload.number: must be integer")
    pr = _read_pr(agent, sources)
    if pr["state"] != "OPEN":
        raise SchemaError(f"payload: {agent['repo']}#{agent['number']} is {pr['state']}, not OPEN")
    if pr["head_sha"] != agent["head_sha"]:
        raise SchemaError(f"payload.head_sha: the PR head is {pr['head_sha'][:12]}, not "
                          f"{str(agent['head_sha'])[:12]}; read the PR again and park its current head")
    method = agent.get("method")
    if method is None:
        try:
            method = sources.github_merge_method(agent["repo"])
        except SourceUnavailable as exc:
            raise SchemaError(f"payload.method: could not read the repo default ({exc})") from exc
    return {
        "repo": agent["repo"], "number": agent["number"], "head_sha": agent["head_sha"],
        "title": plain(pr["title"]).replace("\n", " ")[:300], "tldr": extract_tldr(pr["body"]),
        "base_ref": pr["base_ref"], "head_ref": pr["head_ref"], "method": method,
        "admin": needs_admin(pr), "promotion": is_promotion(pr),
        "commits": pr["commits"], "files": pr["files"],
    }


def _live_check(payload: dict, sources: LiveSources) -> dict:
    try:
        pr = sources.github_pr(payload["repo"], payload["number"])
    except SourceUnavailable as exc:
        return live_unavailable(str(exc))
    drift = []
    if pr["state"] != "OPEN":
        drift.append(f"state {pr['state']}")
    if pr["head_sha"] != payload["head_sha"]:
        drift.append(f"head moved {payload['head_sha'][:8]} \u2192 {pr['head_sha'][:8]}")
    if pr["base_ref"] != payload["base_ref"]:
        drift.append(f"base changed {payload['base_ref']} \u2192 {pr['base_ref']}")
    if pr["head_ref"] != payload["head_ref"]:
        drift.append(f"head ref changed {payload['head_ref']} \u2192 {pr['head_ref']}")
    if is_promotion(pr) != payload["promotion"]:
        drift.append("promotion changed")
    checks = pr["checks"]
    if checks["red"]:
        drift.append("CI red at head")
    return live_result(
        drift, head_matches=pr["head_sha"] == payload["head_sha"],
        base_matches=pr["base_ref"] == payload["base_ref"],
        checks=f"{checks['green']}/{checks['total']}",
        checks_green=checks["red"] == 0 and checks["pending"] == 0,
        mergeable=pr["mergeable"], merge_state=pr["merge_state"], review_decision=pr["review_decision"],
        unverified=["review_condition", "bot_threads"])


def _legacy(reason: str) -> Optional[dict]:
    gate = legacy.parse_gate(reason)
    if not gate or gate[0] != "merge":
        return None
    url = legacy.PR_URL_RE.search(gate[1] or "")
    head = gate[2].get("head", "")
    if not url or not re.fullmatch(r"[0-9a-f]{40}", head):
        return None
    agent = {"repo": url["repo"], "number": int(url["number"]), "head_sha": head}
    if gate[2].get("method") in ("squash", "merge", "rebase"):
        agent["method"] = gate[2]["method"]
    return agent


def _park(env: dict) -> tuple[str, list[tuple[str, str]]]:
    p = env["payload"]
    return (f"https://github.com/{p['repo']}/pull/{p['number']}",
            [("head", p["head_sha"]), ("base", p["base_ref"]), ("method", p["method"])])


SPEC = KindSpec(
    kind=KIND, park_tag="awaiting-approval", gate="merge",
    schema_versions=load_versions(KIND, 1), agent_fields=AGENT_FIELDS,
    binding_fields=("repo", "number", "head_sha", "base_ref", "head_ref", "method", "admin", "promotion"),
    allowed_actions=("approve", "reject", "snooze", "later", "batch"),
    executor="merge-executor", enrich=_enrich, live_check=_live_check, legacy_parse=_legacy,
    park_params=_park, default_then="complete", batchable=True,
)
