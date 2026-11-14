"""``hermes mcp login`` authorizes a server whose tool listing is public (Deel's hosted MCP).

Deel answers ``initialize`` and ``tools/list`` without auth and 401s only on ``tools/call``, so the
login probe never hit the SDK's 401 branch and login ended with "no OAuth token was obtained". A
server that publishes RFC 9728 protected-resource metadata now gets the provider's own flow run up
front (discovery, DCR, PKCE S256, ``oauth.scope``, browser + loopback callback, token on disk).
A server without that metadata keeps the old warning; one that 401s on ``initialize`` still runs
exactly one flow.

End to end against a real local HTTP server: the real probe, provider, callback listener and token
storage; only the browser is replaced by a client that follows the authorize redirect.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

pytest.importorskip("mcp.client.auth.oauth2")

SCOPE = "organizations:read legal-entity:read"
PUBLIC_TOOL = {"name": "public_tool", "description": "Listed without auth", "inputSchema": {"type": "object"}}
PRIVATE_TOOL = {"name": "private_tool", "description": "Listed with a token", "inputSchema": {"type": "object"}}


class _FakeDeel:
    """MCP resource + authorization server on one loopback origin.

    ``mode``: ``public`` (Deel: listing public, PRM published), ``no_prm`` (listing public, no PRM),
    ``protected`` (``initialize`` needs a token, PRM published)."""

    def __init__(self, mode: str):
        self.mode = mode
        self.authorize_hits: list[dict] = []
        self.registrations: list[dict] = []
        self.token_requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output clean
                pass

            def _send(self, status, body=None, headers=None, ctype="application/json"):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                if body is not None:
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self):
                return self.headers.get("Authorization") == "Bearer AT-1"

            def _challenge(self):
                self._send(401, {"error": "unauthorized"}, {
                    "WWW-Authenticate": f'Bearer resource_metadata="{fake.base}/.well-known/oauth-protected-resource/mcp"'})

            def do_GET(self):
                path, query = urlsplit(self.path).path, parse_qs(urlsplit(self.path).query)
                if path == "/.well-known/oauth-protected-resource/mcp" and fake.mode != "no_prm":
                    return self._send(200, {"resource": f"{fake.base}/mcp", "authorization_servers": [fake.base],
                                            "scopes_supported": ["organizations:read", "legal-entity:read",
                                                                 "organizations:write"]})
                if path == "/.well-known/oauth-authorization-server" and fake.mode != "no_prm":
                    return self._send(200, {
                        "issuer": fake.base, "authorization_endpoint": f"{fake.base}/oauth/authorize",
                        "token_endpoint": f"{fake.base}/oauth/tokens",
                        "registration_endpoint": f"{fake.base}/oauth/register",
                        "response_types_supported": ["code"],
                        "grant_types_supported": ["authorization_code", "refresh_token"],
                        "code_challenge_methods_supported": ["S256"],
                        "token_endpoint_auth_methods_supported": ["none"]})
                if path == "/oauth/authorize":
                    params = {k: v[0] for k, v in query.items()}
                    fake.authorize_hits.append(params)
                    target = params["redirect_uri"] + "?" + urlencode({"code": "CODE-1", "state": params["state"]})
                    return self._send(302, headers={"Location": target})
                if path == "/mcp":
                    return self._send(405, {"error": "no SSE stream"})
                return self._send(404, {"error": "not found"})

            def do_DELETE(self):
                self._send(200, {})

            def do_POST(self):
                path = urlsplit(self.path).path
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if path == "/oauth/register":
                    body = json.loads(raw)
                    fake.registrations.append(body)
                    return self._send(201, {"client_id": "dcr-client", "redirect_uris": body["redirect_uris"],
                                            "token_endpoint_auth_method": "none",
                                            "grant_types": ["authorization_code", "refresh_token"],
                                            "response_types": ["code"]})
                if path == "/oauth/tokens":
                    form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                    fake.token_requests.append(form)
                    challenge = fake.authorize_hits[-1]["code_challenge"] if fake.authorize_hits else None
                    digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
                    if challenge != base64.urlsafe_b64encode(digest).rstrip(b"=").decode():
                        return self._send(400, {"error": "invalid_grant"})
                    return self._send(200, {"access_token": "AT-1", "token_type": "Bearer", "expires_in": 3600,
                                            "refresh_token": "RT-1",
                                            "scope": fake.authorize_hits[-1].get("scope")})
                if path == "/mcp":
                    return self._mcp(json.loads(raw))
                return self._send(404, {"error": "not found"})

            def _mcp(self, message):
                method = message.get("method")
                if method == "initialize":
                    if fake.mode == "protected" and not self._authorized():
                        return self._challenge()
                    return self._send(200, {"jsonrpc": "2.0", "id": message["id"], "result": {
                        "protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-deel", "version": "1.0"}}},
                        {"Mcp-Session-Id": "session-1"})
                if "id" not in message:
                    return self._send(202)
                if method == "tools/call" and not self._authorized():
                    return self._challenge()
                if fake.mode == "protected" and not self._authorized():
                    return self._challenge()
                result = {}
                if method == "tools/list":
                    result = {"tools": [PUBLIC_TOOL, PRIVATE_TOOL] if self._authorized() else [PUBLIC_TOOL]}
                return self._send(200, {"jsonrpc": "2.0", "id": message["id"], "result": result})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def login(monkeypatch, tmp_path):
    """Run ``_reauth_oauth_server`` against a fake server; the browser follows the authorize URL."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import mcp_oauth
    from tools.mcp_oauth_manager import reset_manager_for_tests

    from tests.waiting import wait_until

    reset_manager_for_tests()
    opened: list[str] = []

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    no_follow = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _deliver(callback_url):
        try:
            return no_proxy.open(callback_url, timeout=5).status == 200
        except OSError:  # listener not bound yet: the SDK opens the browser before it starts waiting
            return False

    def _consent(url):
        try:
            no_follow.open(url, timeout=10)
        except urllib.error.HTTPError as redirect:  # the 302 to the loopback callback
            callback_url = redirect.headers["Location"]
        wait_until(lambda: _deliver(callback_url), timeout=15, what="the OAuth callback listener")

    def _browser(url):
        opened.append(url)
        threading.Thread(target=_consent, args=(url,), daemon=True).start()
        return True

    monkeypatch.setattr(mcp_oauth.webbrowser, "open", _browser)
    monkeypatch.setattr(mcp_oauth, "_can_open_browser", lambda: True)
    servers: list[_FakeDeel] = []

    def _run(mode: str, oauth_extra: dict | None = None):
        import hermes_cli.mcp_config as mc

        fake = _FakeDeel(mode)
        servers.append(fake)
        cfg = {"url": f"{fake.base}/mcp", "auth": "oauth", "connect_timeout": 20,
               "oauth": {"scope": SCOPE, "redirect_port": _free_port(), "timeout": 20, **(oauth_extra or {})}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = mc._reauth_oauth_server("deel", cfg)
        return ok, out.getvalue(), fake, opened

    yield _run
    for fake in servers:
        fake.close()
    reset_manager_for_tests()


def _stored_tokens(tmp_path) -> dict | None:
    path = tmp_path / "mcp-tokens" / "deel.json"
    return json.loads(path.read_text()) if path.exists() else None


def test_public_listing_server_with_prm_is_authorized_proactively(login, tmp_path):
    ok, out, fake, opened = login("public")

    assert ok, out
    assert len(opened) == 1 and len(fake.authorize_hits) == 1
    authorize = fake.authorize_hits[0]
    assert authorize["code_challenge_method"] == "S256"
    assert authorize["scope"] == SCOPE  # configured scope, not the PRM's scopes_supported
    assert authorize["client_id"] == "dcr-client" and len(fake.registrations) == 1
    assert fake.token_requests[0]["grant_type"] == "authorization_code"
    tokens = _stored_tokens(tmp_path)
    assert tokens is not None and tokens["access_token"] == "AT-1" and tokens["scope"] == SCOPE
    assert "2 tool(s) available" in out  # re-listed with the token


def test_public_listing_server_without_prm_keeps_the_no_token_warning(login, tmp_path):
    ok, out, fake, opened = login("no_prm")

    assert not ok
    assert "no OAuth token was obtained" in out
    assert opened == [] and fake.authorize_hits == [] and fake.registrations == []
    assert _stored_tokens(tmp_path) is None


def test_server_that_challenges_initialize_runs_one_flow(login, tmp_path):
    ok, out, fake, opened = login("protected")

    assert ok, out
    assert len(opened) == 1 and len(fake.authorize_hits) == 1 and len(fake.registrations) == 1
    assert _stored_tokens(tmp_path)["access_token"] == "AT-1"
