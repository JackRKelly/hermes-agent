"""A strict validator for the JSON Schema (draft 2020-12) subset the kind schemas use.

``jsonschema`` is not a core dependency, and the add-time gate must never silently skip a
constraint, so this module validates the keywords below and REFUSES to load a schema that uses any
other one. The ``x-*`` keywords are annotations a standard validator ignores; they carry the §5.4
text rules (``x-text``: plain | line | text | text-line | markdown, ``x-allow-blank``), the link
allowlist (``x-link``) and ISO-8601-with-offset times (``x-iso``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from tools.action_item_kinds.core import SchemaError, check_iso, check_link_url, check_text

_ANNOTATIONS = {"$schema", "$id", "title", "description", "$defs", "$comment"}
_KEYWORDS = _ANNOTATIONS | {
    "type", "properties", "required", "additionalProperties", "enum", "const", "minLength",
    "maxLength", "pattern", "minimum", "maximum", "items", "minItems", "maxItems", "$ref",
    "x-text", "x-allow-blank", "x-link", "x-iso",
}
_TEXT_MODES = {"plain", "line", "text", "text-line", "markdown"}
_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def load_schema(path: Path) -> dict:
    schema = json.loads(path.read_text(encoding="utf-8"))
    _check_keywords(schema, str(path.name))
    return schema


def _check_keywords(node: Any, where: str) -> None:
    if not isinstance(node, dict):
        raise ValueError(f"{where}: a schema must be an object")
    unknown = sorted(set(node) - _KEYWORDS)
    if unknown:
        raise ValueError(f"{where}: unsupported schema keywords {unknown}")
    if "x-text" in node and node["x-text"] not in _TEXT_MODES:
        raise ValueError(f"{where}: x-text must be one of {sorted(_TEXT_MODES)}")
    if "properties" in node and node.get("additionalProperties") is not False:
        raise ValueError(f"{where}: an object with properties must set additionalProperties: false")
    for key, sub in (node.get("properties") or {}).items():
        _check_keywords(sub, f"{where}.{key}")
    for key, sub in (node.get("$defs") or {}).items():
        _check_keywords(sub, f"{where}.$defs.{key}")
    if "items" in node:
        _check_keywords(node["items"], f"{where}[]")


def validate(value: Any, schema: dict, *, link_hosts: Iterable[str], where: str = "payload") -> None:
    """Raise ``SchemaError`` naming the first failing field."""
    _validate(value, schema, schema, frozenset(link_hosts), where)


def _resolve(node: dict, root: dict) -> dict:
    ref = node.get("$ref")
    if not ref:
        return node
    if not ref.startswith("#/$defs/"):
        raise ValueError(f"only local #/$defs refs are supported ({ref})")
    return root["$defs"][ref[len("#/$defs/"):]]


def _validate(value: Any, node: dict, root: dict, hosts: frozenset, where: str) -> None:
    node = _resolve(node, root)
    types = node.get("type")
    if types is not None:
        names = [types] if isinstance(types, str) else list(types)
        if not any(_TYPES[name](value) for name in names):
            raise SchemaError(f"{where}: must be {' or '.join(names)}")
    if "const" in node and value != node["const"]:
        raise SchemaError(f"{where}: must be {node['const']!r}")
    if "enum" in node and value not in node["enum"]:
        raise SchemaError(f"{where}: must be one of {node['enum']}")
    if isinstance(value, str):
        _validate_string(value, node, hosts, where)
    elif isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]:
            raise SchemaError(f"{where}: must be >= {node['minimum']}")
        if "maximum" in node and value > node["maximum"]:
            raise SchemaError(f"{where}: must be <= {node['maximum']}")
    elif isinstance(value, list):
        _validate_array(value, node, root, hosts, where)
    elif isinstance(value, dict):
        _validate_object(value, node, root, hosts, where)


def _validate_string(value: str, node: dict, hosts: frozenset, where: str) -> None:
    if "minLength" in node and len(value) < node["minLength"]:
        raise SchemaError(f"{where}: at least {node['minLength']} chars")
    if "maxLength" in node and len(value) > node["maxLength"]:
        raise SchemaError(f"{where}: {len(value)} chars, max {node['maxLength']}")
    if "pattern" in node and not re.search(node["pattern"], value):
        raise SchemaError(f"{where}: does not match {node['pattern']} ({value!r})")
    if "x-text" in node:
        check_text(value, node["x-text"], where, allow_blank=bool(node.get("x-allow-blank")),
                   link_hosts=hosts)
    if node.get("x-link"):
        check_link_url(value, where, hosts)
    if node.get("x-iso"):
        check_iso(value, where)


def _validate_array(value: list, node: dict, root: dict, hosts: frozenset, where: str) -> None:
    if "minItems" in node and len(value) < node["minItems"]:
        raise SchemaError(f"{where}: needs at least {node['minItems']} items")
    if "maxItems" in node and len(value) > node["maxItems"]:
        raise SchemaError(f"{where}: at most {node['maxItems']} items")
    if "items" in node:
        for i, item in enumerate(value):
            _validate(item, node["items"], root, hosts, f"{where}[{i}]")


def _validate_object(value: dict, node: dict, root: dict, hosts: frozenset, where: str) -> None:
    props = node.get("properties")
    if props is not None:
        unknown = sorted(set(value) - set(props))
        if unknown:
            raise SchemaError(f"{where}: unknown keys {unknown}")
    missing = sorted(set(node.get("required") or ()) - set(value))
    if missing:
        raise SchemaError(f"{where}: missing keys {missing}")
    for key, sub in (props or {}).items():
        if key in value:
            _validate(value[key], sub, root, hosts, f"{where}.{key}")
