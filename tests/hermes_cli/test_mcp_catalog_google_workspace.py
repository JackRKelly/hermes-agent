"""Contract tests for the shipped ``google_workspace`` catalog entry.

The entry must stay draft-only for Gmail and read-only for Drive: the server's
``--permissions`` spec decides which OAuth scopes are requested and which tools
are registered, so these tests pin the spec's *meaning* (no send, organize or
full Gmail level; Drive readonly; no Calendar; no flag that bypasses
permission mode) rather than a snapshot of the manifest. They also install the
real manifest into a temp HERMES_HOME and check what lands in config.yaml.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ENTRY = "google_workspace"
SOURCE_CATALOG = Path(__file__).parents[2] / "optional-mcps"
# Gmail levels at or above these grant gmail.send, gmail.modify or
# gmail.settings.basic (taylorwilsdon/google_workspace_mcp auth/permissions.py).
FORBIDDEN_GMAIL_LEVELS = {"organize", "send", "full"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    hh = tmp_path / "hermes-home"
    hh.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hh))
    monkeypatch.setenv("HERMES_OPTIONAL_MCPS", str(SOURCE_CATALOG))
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: hh)
    monkeypatch.setattr("hermes_cli.config.get_config_path", lambda: hh / "config.yaml")
    monkeypatch.setattr("hermes_cli.config.get_env_path", lambda: hh / ".env")
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: hh)
    return hh


@pytest.fixture
def entry(home):
    from hermes_cli.mcp_catalog import get_entry

    found = get_entry(ENTRY)
    assert found is not None, "google_workspace catalog entry missing or invalid"
    return found


def _permissions(args: list[str]) -> dict[str, set[str]]:
    """``--permissions svc:lvl[+lvl] ...`` -> {svc: {levels}} (nargs='+' runs to the next flag)."""
    start = args.index("--permissions") + 1
    perms: dict[str, set[str]] = {}
    for spec in args[start:]:
        if spec.startswith("-"):
            break
        service, _, level = spec.partition(":")
        assert service not in perms, f"duplicate service {service!r}"
        perms[service] = set(level.split("+"))
    return perms


def test_permissions_grant_gmail_drafts_and_drive_read_only(entry):
    args = entry.transport.args
    assert args.count("--permissions") == 1
    perms = _permissions(args)

    assert set(perms) == {"gmail", "drive"}, "only Gmail and Drive; Calendar has its own MCP"
    assert perms["gmail"] == {"readonly", "drafts"}
    assert not perms["gmail"] & FORBIDDEN_GMAIL_LEVELS
    assert perms["drive"] == {"readonly"}
    # Flags that would replace or bypass permission-mode filtering.
    assert not {"--tools", "--read-only", "--tool-tier"} & set(args)
    assert args[args.index("--transport") + 1] == "stdio"
    assert "--single-user" in args


def test_send_tools_are_excluded_by_default(entry):
    excluded = entry.tools.default_excluded or []
    assert "send_gmail_message" in excluded
    assert "send_*" in excluded


def test_install_writes_draft_only_server_with_shared_token_dir(entry, home, monkeypatch, tmp_path):
    import hermes_cli.mcp_catalog as mc
    from hermes_cli.config import load_config
    from hermes_cli.mcp_security import validate_mcp_server_entry

    clone = tmp_path / "clone"
    monkeypatch.setattr(mc, "_do_git_install", lambda e: clone)
    monkeypatch.setattr(mc, "_probe_tools", lambda name: pytest.fail("exclude-mode install must not probe"))

    mc.install_entry(entry, enable=True, preloaded_env={
        "GOOGLE_OAUTH_CLIENT_ID": "id.apps.googleusercontent.com",
        "GOOGLE_OAUTH_CLIENT_SECRET": "not-a-real-secret",
        "USER_GOOGLE_EMAIL": "owner@example.com",
    })

    server = load_config()["mcp_servers"][ENTRY]
    assert validate_mcp_server_entry(ENTRY, server) == []
    assert server["command"] == "uv"
    assert str(clone) in server["args"]
    assert server["tools"]["exclude"] == entry.tools.default_excluded

    env = server["env"]
    # Secrets stay references resolved from .env; only the non-secret email is inlined.
    assert env["GOOGLE_OAUTH_CLIENT_ID"] == "${GOOGLE_OAUTH_CLIENT_ID}"
    assert env["GOOGLE_OAUTH_CLIENT_SECRET"] == "${GOOGLE_OAUTH_CLIENT_SECRET}"
    assert env["USER_GOOGLE_EMAIL"] == "owner@example.com"
    assert "not-a-real-secret" not in (home / "config.yaml").read_text(encoding="utf-8")

    # One fixed token dir for every profile: a literal path, not derived from the installing
    # profile's home, and never a profile's own Google token.
    creds = env["WORKSPACE_MCP_CREDENTIALS_DIR"]
    assert "${" not in creds and str(home) not in creds
    assert Path(creds).expanduser().is_absolute()
    assert "profiles" not in Path(creds).parts and "google_token" not in creds
