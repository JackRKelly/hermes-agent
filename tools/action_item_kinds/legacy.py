"""Parse a free-text park reason the way assistant ``scripts/kanban_gates.py`` does.

The same regexes as ``GATE_RE`` / ``KV_RE`` there, so a legacy park reads identically on both
sides. Used by each kind's ``legacy_parse`` and by the tools to read an item's park parameters.
"""

from __future__ import annotations

import re
from typing import Optional

GATE_RE = re.compile(r"\[awaiting-approval:(?P<gate>[a-z-]+)\]\s*(?P<rest>[^\n]*)")
NEEDS_JACK_RE = re.compile(r"\[needs-jack(?::(?P<what>[\w-]+))?\]\s*(?P<rest>[^\n]*)", re.I)
KV_RE = re.compile(r"(?<!\S)([a-z]+)=(\S+)")
KV_TRAILING = ".,;:!?)]}>\"'"
URL_RE = re.compile(r"https?://\S+")
PR_URL_RE = re.compile(r"https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<number>\d+)")


def _params(rest: str) -> dict[str, str]:
    return {key: value.rstrip(KV_TRAILING) for key, value in KV_RE.findall(rest)}


def parse_gate(reason: Optional[str]) -> Optional[tuple[str, str, dict[str, str]]]:
    """``(gate, artifact, params)`` of an ``[awaiting-approval:<gate>]`` park, else None."""
    match = GATE_RE.search(reason or "")
    if not match:
        return None
    rest = match["rest"].strip()
    url = URL_RE.search(rest)
    artifact = url.group(0) if url else KV_RE.sub("", rest).strip()
    return match["gate"], artifact, _params(rest)


def parse_needs_jack(reason: Optional[str]) -> Optional[tuple[str, str, dict[str, str]]]:
    """``(what, text, params)`` of a ``[needs-jack:<what>]`` park, else None."""
    match = NEEDS_JACK_RE.search(reason or "")
    if not match:
        return None
    rest = match["rest"].strip()
    return (match["what"] or "").lower(), KV_RE.sub("", rest).strip(), _params(rest)
