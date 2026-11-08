"""A kanban card deep link survives the dashboard login gate.

An unauthenticated GET of ``/kanban?profile=default&task=<id>&board=<slug>`` must carry the
FULL path + query (every param, URL-encoded once) in ``next=`` on both gate exits — the
``/login`` interstitial and the single-provider auto-SSO hop — so the user lands back on the
card after signing in. Drives the real ``gated_auth_middleware`` through a ``TestClient``.
"""
from __future__ import annotations

from urllib.parse import parse_qs, quote, urlsplit

import pytest

from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.cookies import SSO_ATTEMPT_COOKIE
from hermes_cli.dashboard_auth.routes import _validate_post_login_target
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider

DEEP_LINK = "/kanban?profile=default&task=t_x&board=b"
ENCODED = quote(DEEP_LINK, safe="")


@pytest.fixture
def gated_app():
    clear_providers()
    register_provider(StubAuthProvider())
    prev = {k: getattr(web_server.app.state, k, None) for k in ("bound_host", "bound_port", "auth_required")}
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    yield TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    clear_providers()
    for k, v in prev.items():
        setattr(web_server.app.state, k, v)


def test_login_redirect_keeps_full_kanban_deep_link(gated_app):
    # Guard cookie present -> the gate falls back to the /login interstitial.
    gated_app.cookies.set(SSO_ATTEMPT_COOKIE, "1")
    r = gated_app.get(DEEP_LINK, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"/login?next={ENCODED}"
    # One decode yields the exact deep link, and the post-login validator keeps it.
    nxt = parse_qs(urlsplit(r.headers["location"]).query)["next"][0]
    assert nxt == DEEP_LINK
    assert _validate_post_login_target(nxt) == DEEP_LINK


def test_auto_sso_redirect_keeps_full_kanban_deep_link(gated_app):
    r = gated_app.get(DEEP_LINK, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"/auth/login?provider=stub&next={ENCODED}"
