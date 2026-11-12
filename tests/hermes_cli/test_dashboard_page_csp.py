"""The dashboard's SPA document runs only its own bundle and the server's bootstrap script.

The dashboard is one document: every route (``/chat``, ``/action-items``, a plugin tab) is
client-side, so the policy on the HTML ``_serve_index`` returns governs all of them.
"""
import base64
import hashlib
from html.parser import HTMLParser

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _InlineScripts(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=False)
        self.inline = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "script" and "src" not in dict(attrs):
            self.current = ""

    def handle_data(self, data):
        if self.current is not None:
            self.current += data

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.inline.append(self.current)
            self.current = None


def _directives(response) -> dict:
    return dict(part.strip().split(" ", 1) for part in response.headers["content-security-policy"].split(";"))


def _sha(script: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'"


@pytest.fixture
def dashboard(monkeypatch, tmp_path):
    from hermes_cli import web_server as server
    from hermes_cli.web_server_dashboard import mount_spa

    (tmp_path / "index.html").write_text(
        '<html><head><script type="module" src="/assets/app.js"></script></head><body></body></html>')
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/app.js").write_text("export const loaded = true;")
    (tmp_path / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
    app = FastAPI()
    app.state.auth_required = False
    app.state.web_dist = tmp_path
    monkeypatch.setattr(server, "app", app)
    monkeypatch.delenv("HERMES_SERVE_HEADLESS", raising=False)
    mount_spa(app)
    return app


@pytest.mark.parametrize("surface", [None, "dashboard"], ids=["unset", "dashboard"])
@pytest.mark.parametrize("prefix", ["", "/hermes"])
@pytest.mark.parametrize("path", ["/", "/action-items", "/chat"])
def test_dashboard_document_authorizes_only_its_bootstrap_and_bundle(dashboard, surface, prefix, path):
    if surface is not None:
        dashboard.state.ui_surface = surface
    response = TestClient(dashboard).get(path, headers={"X-Forwarded-Prefix": prefix})

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    policy = _directives(response)
    assert set(policy) == {"script-src", "script-src-attr", "object-src", "base-uri", "frame-ancestors"}
    assert policy["script-src-attr"] == policy["object-src"] == policy["base-uri"] == "'none'"
    assert policy["frame-ancestors"] == "'none'"
    scripts = policy["script-src"].split()
    assert "'self'" in scripts  # the bundle and same-origin plugin bundles
    assert not {"'unsafe-inline'", "'unsafe-eval'", "blob:", "https:", "http:", "*"}.intersection(scripts)
    # The bootstrap varies with token, profile and prefix: its hash is taken from what was served.
    inline = _InlineScripts(response.text).inline
    assert any(f'__HERMES_BASE_PATH__="{prefix}"' in script for script in inline)
    assert {_sha(script) for script in inline} == {s for s in scripts if s.startswith("'sha256-")}
    assert _sha("window.injected=true") not in scripts


@pytest.mark.parametrize("gated", [False, True])
@pytest.mark.parametrize("path", ["/index.html", "/%2e/index.html"])
def test_the_built_entry_document_cannot_escape_the_policy(dashboard, gated, path):
    """``/index.html`` stays the built file on the dashboard, and under the auth gate it is a working
    dashboard by cookie, so it carries the same policy as every other spelling of the document."""
    dashboard.state.auth_required = gated
    response = TestClient(dashboard).get(path)

    assert response.status_code == 200
    assert response.text == (dashboard.state.web_dist / "index.html").read_text()
    assert response.headers["x-content-type-options"] == "nosniff"
    policy = _directives(response)
    assert policy["script-src"].split() == ["'self'"]  # the built file has no inline script
    assert policy["script-src-attr"] == policy["object-src"] == policy["base-uri"] == "'none'"
    assert policy["frame-ancestors"] == "'none'"


def test_the_webapp_keeps_its_own_policy(dashboard):
    dashboard.state.ui_surface = "webapp"
    policy = _directives(TestClient(dashboard).get("/"))

    assert policy["default-src"] == "'none'"
    assert {"'self'", "blob:", "'wasm-unsafe-eval'"} <= set(policy["script-src"].split())
    assert policy["form-action"] == "'none'"


@pytest.mark.parametrize("path", ["/assets/app.js", "/favicon.ico"])
def test_static_assets_carry_no_document_policy(dashboard, path):
    response = TestClient(dashboard).get(path)

    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
