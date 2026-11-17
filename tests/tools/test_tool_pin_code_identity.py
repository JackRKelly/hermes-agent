"""A session's tools[] pin is keyed by the code that built it, so a new build re-reads changed schemas.

Regression: the pin was stamped with ``build_info.get_code_identity``, a frozen old-updater shim that
never knows the sha, so every build of one release stamped the bare release version. After a deploy
that added ``title``/``summary`` to ``action_item_add`` (and made the handler require them), a long-lived
gateway session restored its pre-deploy schema byte for byte: the model could not see the parameters
the live handler refused it for missing.
"""
from __future__ import annotations

import copy
import types

import tools.action_item_tools  # registers action_item_add
from hermes_cli import __version__
from tools import mcp_tool_agent as _mcp_agent
from tools.registry import registry

NEW_BUILD = "c" * 40


def _current_def(name: str) -> dict:
    entry = registry.get_entry(name)
    assert entry is not None
    return {"type": "function", "function": {**entry.schema, "name": entry.name}}


def _without_envelope_text(defn: dict) -> dict:
    """``action_item_add`` as the build before the title/summary change sent it."""
    old = copy.deepcopy(defn)
    params = old["function"]["parameters"]
    for key in ("title", "summary"):
        params["properties"].pop(key)
        params["required"].remove(key)
    return old


def test_a_pin_from_an_earlier_build_of_the_same_release_takes_the_new_schema(monkeypatch):
    monkeypatch.setattr("hermes_cli.version_info.get_code_identity",
                        lambda refresh=False: {"sha": NEW_BUILD, "short_sha": NEW_BUILD[:8],
                                               "version": __version__, "source": "git", "commit_date": None})
    monkeypatch.setattr(_mcp_agent, "persist_agent_tool_names", lambda agent: None)
    current = _current_def("action_item_add")
    stale_pin = {"version": __version__, "tools": [_without_envelope_text(current)]}
    agent = types.SimpleNamespace(tools=[current], valid_tool_names={"action_item_add"},
                                  enabled_toolsets=None, disabled_toolsets=None)

    _mcp_agent.restore_agent_tool_prefix(agent, stale_pin)

    assert _mcp_agent.tool_pin_version() == NEW_BUILD
    sent = {t["function"]["name"]: t["function"] for t in agent.tools}["action_item_add"]
    assert {"title", "summary"} <= set(sent["parameters"]["properties"])
    assert {"title", "summary"} <= set(sent["parameters"]["required"])


def test_a_pin_from_this_build_keeps_its_bytes(monkeypatch):
    """The freeze still holds within one build: what the session already sent is sent again."""
    monkeypatch.setattr("hermes_cli.version_info.get_code_identity",
                        lambda refresh=False: {"sha": NEW_BUILD, "short_sha": NEW_BUILD[:8],
                                               "version": __version__, "source": "git", "commit_date": None})
    monkeypatch.setattr(_mcp_agent, "persist_agent_tool_names", lambda agent: None)
    current = _current_def("action_item_add")
    sent_before = copy.deepcopy(current)
    sent_before["function"]["description"] = "as this session first sent it"
    agent = types.SimpleNamespace(tools=[current], valid_tool_names={"action_item_add"},
                                  enabled_toolsets=None, disabled_toolsets=None)

    _mcp_agent.restore_agent_tool_prefix(agent, {"version": NEW_BUILD, "tools": [sent_before]})

    assert agent.tools == [sent_before]


def test_without_a_sha_the_pin_is_the_release(monkeypatch):
    """No stamp and no git: the resolver reports version "unknown"; keying on that would never move."""
    monkeypatch.setattr("hermes_cli.version_info.get_code_identity",
                        lambda refresh=False: {"sha": None, "short_sha": None, "version": "unknown",
                                               "source": "unknown", "commit_date": None})

    assert _mcp_agent.tool_pin_version() == __version__
