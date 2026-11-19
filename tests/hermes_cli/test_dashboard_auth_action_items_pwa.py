"""The Action Items PWA shell answers without a session; its queue and decisions stay gated.

The shell (``/api/plugins/action-items/app/...``), its manifest and its home-screen icons are static
files with no queue data in them, so a signed-out app can load and show its own sign-in link instead of
a blank page or a raw 401 body. Everything else under ``/api/plugins/action-items/`` (``/items``,
``/items/{id}/decision``, ...) still needs a session, and look-alike paths are not public.

The plugin itself lives outside this repository, so a stand-in router with the same route shapes is
mounted ahead of the SPA catch-all for the duration of each test.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import APIRouter, Response
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.middleware import _path_is_public
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider

BASE = "/api/plugins/action-items"
SECRET = b"queue-data-must-not-leak"

SHELL_PATHS = [f"{BASE}/app/", f"{BASE}/app/sw.js", f"{BASE}/app/index.js", f"{BASE}/app/style.css",
               f"{BASE}/app/vendor-preact.js"]
ICON_PATHS = [f"{BASE}/app.webmanifest", f"{BASE}/icon-180.png", f"{BASE}/icon-192.png",
              f"{BASE}/icon-512.png"]
GATED_PATHS = [f"{BASE}/items", f"{BASE}/items/x", f"{BASE}/csrf", f"{BASE}/history", f"{BASE}/live",
               f"{BASE}/summary", f"{BASE}/sw.js", f"{BASE}/app"]
LOOKALIKE_PATHS = [f"{BASE}/appx", f"{BASE}/appx/index.js", f"{BASE}/app.webmanifestx",
                   f"{BASE}/icon-180.png.map", f"{BASE}/icon-999.png", f"{BASE}/app.webmanifest/x",
                   "/api/plugins/action-itemsx/app/", "/api/plugins/other/app/"]


def _stand_in_router() -> APIRouter:
    """The plugin's route shapes: static shell/icons, and data routes that would leak ``SECRET``."""
    router = APIRouter(prefix=BASE)

    @router.get("/app/")
    def shell():
        return Response("<!doctype html><title>Action Items</title>", media_type="text/html")

    @router.get("/app/sw.js")
    def shell_sw():
        return Response(b"// sw", media_type="text/javascript")

    @router.get("/app/{name}")
    def shell_asset(name: str):
        if name not in ("index.js", "style.css", "vendor-preact.js"):
            return Response(status_code=404)
        return Response(b"// asset", media_type="text/javascript")

    @router.get("/app.webmanifest")
    def manifest():
        return Response(b"{}", media_type="application/manifest+json")

    @router.get("/icon-{size}.png")
    def icon(size: int):
        return Response(b"\x89PNG", media_type="image/png")

    for data_path in ("/items", "/items/{item_id}", "/csrf", "/history", "/live", "/summary", "/sw.js",
                      "/app"):
        router.add_api_route(data_path, lambda: Response(SECRET), methods=["GET"])

    @router.post("/items/{item_id}/decision")
    def decision(item_id: str):
        return Response(SECRET)

    return router


@pytest.fixture
def gated(monkeypatch):
    clear_providers()
    register_provider(StubAuthProvider())
    for key, value in (("bound_host", "fly-app.fly.dev"), ("bound_port", 443), ("auth_required", True)):
        monkeypatch.setattr(web_server.app.state, key, value, raising=False)

    async def _enabled(name):  # the stand-in plugin counts as installed and enabled
        return False

    monkeypatch.setattr(web_server, "_plugin_api_request_refused", _enabled)
    routes = web_server.app.router.routes
    saved = list(routes)
    routes[:0] = _stand_in_router().routes
    try:
        yield TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    finally:
        routes[:] = saved
        clear_providers()


@pytest.mark.parametrize("path", SHELL_PATHS + ICON_PATHS)
def test_pwa_shell_and_icons_answer_without_a_session(gated, path):
    response = gated.get(path, follow_redirects=False)

    assert response.status_code == 200, (path, response.status_code, response.headers.get("location"))
    assert SECRET not in response.content


@pytest.mark.parametrize("path", GATED_PATHS + LOOKALIKE_PATHS)
def test_queue_and_lookalikes_still_require_a_session(gated, path):
    response = gated.get(path, follow_redirects=False)

    assert response.status_code in (401, 302), (path, response.status_code)
    assert SECRET not in response.content


def test_decision_still_requires_a_session(gated):
    response = gated.post(f"{BASE}/items/x/decision", json={"verdict": "approve"}, follow_redirects=False)

    assert response.status_code in (401, 403), response.status_code
    assert SECRET not in response.content


@pytest.mark.parametrize("path", GATED_PATHS + LOOKALIKE_PATHS + [f"{BASE}/items/x/decision"])
def test_gate_does_not_treat_queue_or_lookalikes_as_public(path):
    assert not _path_is_public(path)


@pytest.mark.parametrize("path", SHELL_PATHS + ICON_PATHS)
def test_gate_treats_shell_and_icons_as_public(path):
    assert _path_is_public(path)


def _raw_get(path: str) -> tuple[int, bytes]:
    """One GET straight into the ASGI app with ``path`` exactly as sent (an HTTP client would
    collapse ``..`` before sending; ``curl --path-as-is`` and raw sockets do not)."""
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "https", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": b"", "headers": [(b"host", b"fly-app.fly.dev")],
             "client": ("203.0.113.9", 50000), "server": ("fly-app.fly.dev", 443)}
    asyncio.run(web_server.app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


@pytest.mark.parametrize("path", [f"{BASE}/app/../items", f"{BASE}/app/../items/x"])
def test_dot_segment_under_the_shell_never_reaches_queue_data(gated, path):
    """The prefix check passes ``/app/../items`` sent raw, but routing does not collapse ``..``
    and ``/app/{name}`` matches one segment only, so it reaches no data route."""
    status, body = _raw_get(path)

    assert status != 200 or SECRET not in body, (path, status)
    assert SECRET not in body
