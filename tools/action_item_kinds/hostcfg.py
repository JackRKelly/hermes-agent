"""The two dashboard settings action items read from the ROOT config (the shared board root).

Read from ``kanban_home()/config.yaml`` on every call, never a profile's ``HERMES_HOME``: every
profile parks on the same boards and Jack's dashboard is configured once, in the root config.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from ruamel.yaml.error import YAMLError

from tools.action_item_kinds.core import STATIC_LINK_HOSTS


def dashboard_config() -> dict:
    from hermes_cli.config_backend import read_config_doc_readonly
    from hermes_cli.kanban_db import kanban_home

    try:
        # Shared, signature-cached parse: read only, never mutated.
        data = read_config_doc_readonly(kanban_home() / "config.yaml")
    except (OSError, YAMLError, UnicodeDecodeError):
        return {}
    section = data.get("dashboard") if isinstance(data, dict) else None
    return section if isinstance(section, dict) else {}


def link_hosts() -> frozenset[str]:
    """The §5.4 link allowlist: the static hosts plus the dashboard's own ``public_url`` host."""
    host = urlsplit(str(dashboard_config().get("public_url") or "")).hostname
    return STATIC_LINK_HOSTS | ({host} if host else set())


def dashboard_principal() -> Optional[str]:
    """Jack's basic-auth user (§5.2): the only principal a ``source=webapp:`` decision may carry."""
    auth = dashboard_config().get("basic_auth")
    user = auth.get("username") if isinstance(auth, dict) else None
    return user.strip() if isinstance(user, str) and user.strip() else None
