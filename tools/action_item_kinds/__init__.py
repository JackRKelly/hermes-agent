"""Backend registry of action item kinds (JackRKelly/assistant docs/design/action-items-webapp.md §3.2).

One JSON Schema per kind and version (``<kind>.v<N>.schema.json``) plus one ``KindSpec`` per kind
(``<kind>.py``), collected in ``registry.py``. The ``action_item_*`` tools validate with it, and the
dashboard plugin imports it (``from tools.action_item_kinds import KINDS``) so there is no copy to
drift. A library package: it registers no tools (``tools/action_item_tools.py`` does).
"""

from tools.action_item_kinds.core import (
    COMMENT_PREFIX, SchemaError, canonical_json, comment_body, comment_hash16, item_hash,
    parse_comment, prefixed_hash,
)
from tools.action_item_kinds.registry import (
    KINDS, KINDS_VERSION, build_envelope, classify, legacy_payload, park_reason, spec_for,
    validate_envelope,
)
from tools.action_item_kinds.spec import KindSpec, LiveSources, SourceUnavailable

__all__ = [
    "COMMENT_PREFIX", "KINDS", "KINDS_VERSION", "KindSpec", "LiveSources", "SchemaError",
    "SourceUnavailable", "build_envelope", "canonical_json", "classify", "comment_body",
    "comment_hash16", "item_hash", "legacy_payload", "park_reason", "parse_comment", "prefixed_hash",
    "spec_for", "validate_envelope",
]
