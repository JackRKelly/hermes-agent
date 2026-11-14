"""Proactive browser authorization for ``hermes mcp login`` / ``reauth``.

Login normally rides the SDK's 401 branch: the probe's ``initialize`` is challenged and the provider
runs discovery, registration, PKCE and the token exchange. Some servers (Deel's hosted MCP among
them) answer ``initialize`` and ``tools/list`` without auth and challenge only a protected
``tools/call``, so that branch never fires and login ends with no token.

When such a server publishes RFC 9728 protected-resource metadata it has told us it is an OAuth
resource, so this module runs the SAME provider flow up front: it hands the cached
``HermesMCPOAuthProvider`` a synthetic 401 for a never-sent resource request and forwards every
discovery / registration / token request the provider yields to the network. Nothing about the
OAuth client is reimplemented: PRM and authorization-server discovery, DCR or CIMD, PKCE S256, the
browser redirect + callback and ``HermesTokenStorage`` all stay the provider's.

The synthetic challenge names the discovered metadata URL and, when configured, ``oauth.scope``:
the SDK ranks a challenge's scope first, so the configured scope is what gets requested; without
one it falls back to its usual ``scopes_supported`` selection.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def discover_protected_resource_metadata_url(server_url: str, *, ssl_verify: Any = True) -> str | None:
    """URL of the RFC 9728 document *server_url* publishes (path-suffixed form, then root), or None.

    Uses the SDK's own URL builder and response parser, the order its 401 branch walks. Network
    errors and non-200s mean "not published": the caller keeps today's behaviour.
    """
    from mcp.client.auth.utils import (
        build_protected_resource_metadata_discovery_urls, create_oauth_metadata_request,
        handle_protected_resource_response)
    from tools.mcp_oauth_provider import stamp_default_user_agent
    from tools.mcp_tool import sdk_httpx

    httpx = sdk_httpx()
    async with httpx.AsyncClient(timeout=10.0, verify=ssl_verify) as client:
        for url in build_protected_resource_metadata_discovery_urls(None, server_url):
            try:
                response = await client.send(stamp_default_user_agent(create_oauth_metadata_request(url)))
            except httpx.HTTPError as exc:
                logger.debug("MCP OAuth: protected-resource metadata fetch %s failed: %s", url, exc)
                continue
            if await handle_protected_resource_response(response) is not None:
                return url
    return None


def _challenge(metadata_url: str, scope: str | None) -> str:
    """``WWW-Authenticate`` value for the synthetic 401. A scope holding a quote cannot be a
    valid RFC 6749 scope-token list, so it is left out rather than breaking the header."""
    value = f'Bearer resource_metadata="{metadata_url}"'
    if scope and '"' not in scope and "\\" not in scope:
        value += f', scope="{scope}"'
    return value


async def drive_authorization(provider: Any, server_url: str, challenge: str, *, ssl_verify: Any = True) -> None:
    """Run *provider*'s auth flow as if *server_url* had answered 401 with *challenge*.

    The resource request itself is never sent: its first appearance gets the synthetic 401 and its
    retry (the provider now holds tokens) a synthetic 200, which ends the flow normally so the
    provider's locks and persisted metadata settle exactly as after a real request.
    """
    from tools.mcp_tool import sdk_httpx

    httpx = sdk_httpx()
    resource_request = httpx.Request("POST", server_url)
    challenged = False
    flow = provider.async_auth_flow(resource_request)
    # No client-level redirects: the SDK follows same-origin redirects for its own requests.
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), verify=ssl_verify) as client:
        try:
            outgoing = await flow.__anext__()
            while True:
                if outgoing is resource_request:
                    status = 200 if challenged else 401
                    headers = {} if challenged else {"WWW-Authenticate": challenge}
                    challenged = True
                    response = httpx.Response(status, headers=headers, request=resource_request)
                else:
                    response = await client.send(outgoing)
                outgoing = await flow.asend(response)
        except StopAsyncIteration:
            pass
        finally:
            await flow.aclose()


async def proactive_browser_login(name: str, server_config: dict) -> bool:
    """Authorize *name* up front when it publishes protected-resource metadata.

    Returns False when no metadata is published (nothing ran). Returns True once the flow ran; a
    failed flow raises the provider's own error, which the login path already humanizes.
    """
    url = server_config["url"]
    ssl_verify = server_config.get("ssl_verify", True)
    metadata_url = await discover_protected_resource_metadata_url(url, ssl_verify=ssl_verify)
    if metadata_url is None:
        return False
    from tools.mcp_oauth import apply_oauth_provider_defaults
    from tools.mcp_oauth_manager import get_manager

    oauth_cfg = server_config.get("oauth") or {}
    provider = get_manager().get_or_build_provider(name, url, oauth_cfg)
    if provider is None:
        return False
    # The scope the provider was built with (oauth.scope, or a provider default such as Figma's).
    scope = apply_oauth_provider_defaults(dict(oauth_cfg), server_name=name, server_url=url).get("scope")
    logger.info("MCP OAuth '%s': server lists tools without auth but publishes %s; authorizing proactively",
                name, metadata_url)
    await drive_authorization(provider, url, _challenge(metadata_url, scope), ssl_verify=ssl_verify)
    return True
