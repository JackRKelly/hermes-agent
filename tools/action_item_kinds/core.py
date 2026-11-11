"""Canonical JSON, the item hashes and the text rules every action item kind shares.

The byte-level contract (JackRKelly/assistant ``docs/design/action-items-webapp.md`` §3.2.1):

* the item comment is the literal 17-byte prefix ``[action-item:v1] `` followed by
  ``canonical_json(envelope)``;
* the park binds it with ``item=<hash16>``, ``hash16 = sha256(canonical_json(envelope))[:16]``;
* a verifier hashes the exact UTF-8 bytes of the stored comment body after the prefix, never a
  re-serialisation of the parsed JSON, so a re-formatted comment simply does not match.

Stdlib only: the dashboard plugin and the assistant CLI import this module too.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

COMMENT_PREFIX = "[action-item:v1] "
WITHDRAWN_PREFIX = "[action-item-withdrawn"
# Decision prefixes are written only by Jack's verified surfaces (the web app, the approvals
# handler). No agent-facing path may emit one, anywhere in its text.
DECISION_PREFIXES = ("[jack-approved", "[jack-rejected", "[jack-done")
ITEM_RE = re.compile(r"(?<!\S)item=([0-9a-f]{16})(?!\S)")
STATIC_LINK_HOSTS = frozenset({"github.com", "linear.app", "mail.google.com", "app.slack.com"})
EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
# An HTML tag (``<p>``, ``</div>``, ``<br/>``, ``<a href=...>``); ``<dave@x.com>`` is not one.
HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(\s[^<>]*)?/?>")
# Markdown constructs refused in ``text`` fields (only ``other.body`` may use the subset).
MARKDOWN_RE = re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)|```|^\s{0,3}#{1,6}\s|\*\*[^*\n]+\*\*", re.M)
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(([^)\s]*)[^)\n]*\)")


class SchemaError(ValueError):
    """The item does not match its kind's schema; nothing may be written."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prefixed_hash(value: Any) -> str:
    """``sha256:<hex>`` over ``canonical_json(value)``: the form of every stored hash field."""
    return "sha256:" + sha256_hex(canonical_json(value))


def item_hash(envelope: dict) -> str:
    """The park's ``item=`` value for an envelope the writer is about to store."""
    return sha256_hex(canonical_json(envelope))[:16]


def comment_body(envelope: dict) -> str:
    return COMMENT_PREFIX + canonical_json(envelope)


def comment_hash16(body: Optional[str]) -> Optional[str]:
    """``hash16`` of a stored item comment: the bytes after the 17-byte prefix, untrimmed."""
    if not body or not body.startswith(COMMENT_PREFIX):
        return None
    return hashlib.sha256(body[len(COMMENT_PREFIX):].encode("utf-8")).hexdigest()[:16]


def parse_comment(body: Optional[str]) -> Optional[dict]:
    """The envelope a stored item comment carries, or None when it is not one."""
    if not body or not body.startswith(COMMENT_PREFIX):
        return None
    try:
        value = json.loads(body[len(COMMENT_PREFIX):])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def reason_item_hash(reason: Optional[str]) -> Optional[str]:
    """The ``item=`` hash a park reason binds (the last one; the writer puts it last)."""
    found = ITEM_RE.findall(reason or "")
    return found[-1] if found else None


# --- text rules (the schemas name them with ``x-text``) ---

def check_text(value: str, mode: str, where: str, *, allow_blank: bool = False,
               link_hosts: Iterable[str] = STATIC_LINK_HOSTS) -> None:
    """``plain``: no HTML (the frozen v1 rule). ``line``: plain, one line. ``text``: plain and no
    markdown. ``text-line``: text, one line. ``markdown``: the §5.4 subset, HTML refused and every
    markdown link target an allowlisted https URL."""
    if not allow_blank and not value.strip():
        raise SchemaError(f"{where}: must not be empty")
    if mode in ("line", "text-line") and ("\n" in value or "\r" in value):
        raise SchemaError(f"{where}: must be one line")
    if HTML_TAG_RE.search(value):
        raise SchemaError(f"{where}: plain text only, HTML is refused")
    if mode in ("text", "text-line") and MARKDOWN_RE.search(value):
        raise SchemaError(f"{where}: plain text only, markdown is refused here")
    if mode == "markdown":
        for target in MARKDOWN_LINK_RE.findall(value):
            check_link_url(target, f"{where} link", link_hosts)


def check_link_url(url: Any, where: str, hosts: Iterable[str] = STATIC_LINK_HOSTS) -> None:
    allowed = frozenset(hosts)
    parts = urlsplit(url) if isinstance(url, str) else None
    if (not parts or parts.scheme != "https" or (parts.hostname or "") not in allowed
            or parts.username or parts.password or parts.port not in (None, 443)):
        raise SchemaError(f"{where}: only https links to {sorted(allowed)} are allowed ({url!r})")


def check_iso(value: Any, where: str) -> None:
    if not isinstance(value, str):
        raise SchemaError(f"{where}: must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaError(f"{where}: not ISO 8601 ({value!r})") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SchemaError(f"{where}: needs a UTC offset ({value!r})")


def refuse_decision_text(text: str, where: str) -> None:
    """No agent-written text may carry a decision prefix, at the start or anywhere else."""
    lowered = text.lower()
    for prefix in DECISION_PREFIXES:
        if prefix in lowered:
            raise SchemaError(f"{where}: {prefix}...] is a decision marker only Jack's verified "
                              "surfaces write; agents create, update and withdraw items only")
