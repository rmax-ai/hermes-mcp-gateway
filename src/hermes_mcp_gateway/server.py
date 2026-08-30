"""HTTP layer: OAuth 2.1 discovery/token endpoints and the Bearer-authed MCP
Streamable HTTP endpoint, all served from a single Starlette application.
"""

from __future__ import annotations

import base64
import binascii
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from mcp.server.streamable_http_manager import (
    StreamableHTTPASGIApp,
    StreamableHTTPSessionManager,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import auth, tools
from .context import auth_scope

SCOPES_SUPPORTED = [
    "task:run",
    "toolset:file",
    "toolset:web",
    "toolset:search",
    "toolset:terminal",
    "toolset:session_search",
    "toolset:todo",
    "toolset:skills",
    "toolset:memory",
    "toolset:browser",
    "toolset:code_execution",
]

WWW_AUTHENTICATE = (
    'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource", '
    'error="invalid_token"'
)


def _iso_epoch(timestamp: int) -> str:
    return datetime.fromtimestamp(int(timestamp), tz=UTC).isoformat()


def _hostname(host_header: str) -> str:
    # The Host header may include a port; the gateway re-derives the public
    # port from config so discovery documents stay stable.
    return host_header.split(":", 1)[0]


def derive_base_url(host_header: str, cfg) -> str:
    hostname = _hostname(host_header) or cfg.server.bind or "localhost"
    return f"http://{hostname}:{cfg.server.port}"


def _scope_header(scope: dict, name: str) -> str | None:
    target = name.lower().encode("ascii")
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return value.decode("latin-1")
    return None


def _json_error(request: Request, error: str, status: int, description: str | None = None) -> JSONResponse:
    body = {"error": error}
    if description:
        body["error_description"] = description
    return JSONResponse(body, status_code=status)


class McpAuthMiddleware:
    """Validate a Bearer token before handing the request to the MCP handler."""

    def __init__(self, app, cfg, clients_by_id, signing_key: str) -> None:
        self.app = app
        self.cfg = cfg
        self.clients_by_id = clients_by_id
        self.signing_key = signing_key
        self.issuer = cfg.auth.issuer

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        header = _scope_header(scope, "authorization") or ""
        token = None
        if header.lower().startswith("bearer "):
            token = header[7:].strip()

        claims = None
        try:
            claims = auth.validate_token(token, self.signing_key, self.issuer)
        except auth.AuthError:
            claims = None

        client = self.clients_by_id.get(claims["sub"]) if claims else None
        if claims is None or client is None:
            await self._reject(scope, receive, send)
            return

        scope["auth_client_id"] = client.client_id
        scopes = (claims.get("scope") or "").split()
        with auth_scope(client, scopes):
            await self.app(scope, receive, send)

    async def _reject(self, scope, receive, send) -> None:
        base = derive_base_url(_scope_header(scope, "host") or "", self.cfg)
        response = Response(
            content='{"error": "invalid_token"}',
            status_code=401,
            media_type="application/json",
            headers={"WWW-Authenticate": WWW_AUTHENTICATE.format(base=base)},
        )
        await response(scope, receive, send)


class GatewayRouter:
    """Dispatch ``/mcp`` to the authed MCP handler and everything else to Starlette.

    Starlette's ``Mount`` would issue a 307 from ``/mcp`` to ``/mcp/``, which
    the MCP client does not follow; routing here keeps the path intact for both
    ``GET`` and ``POST``. Lifespan events go to the Starlette app so the MCP
    session manager is started and stopped with the server.
    """

    def __init__(self, plain_app, mcp_app) -> None:
        self.plain_app = plain_app
        self.mcp_app = mcp_app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self.plain_app(scope, receive, send)
            return
        if scope["type"] == "http" and scope["path"] == "/mcp":
            await self.mcp_app(scope, receive, send)
            return
        await self.plain_app(scope, receive, send)


class RequestLoggingMiddleware:
    """Print one stderr line per request (timestamp, method, path, client, status)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_box = {"status": None}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_box["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            print(
                f"{datetime.now(UTC).isoformat()} method={scope.get('method')} "
                f"path={scope.get('path')} client_id={scope.get('auth_client_id')} "
                f"status={status_box['status']}",
                file=sys.stderr,
            )


def build_gateway_app(cfg, db, executor, signing_key: str) -> Starlette:
    """Assemble the gateway Starlette application."""
    issuer = cfg.auth.issuer
    clients_by_id = {client.client_id: client for client in cfg.clients}

    mcp_server = tools.build_mcp_server(db, executor)
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server._lowlevel_server,
        json_response=False,
        stateless=False,
    )
    mcp_asgi = StreamableHTTPASGIApp(session_manager)
    authed_mcp = McpAuthMiddleware(mcp_asgi, cfg, clients_by_id, signing_key)

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def oauth_authorization_server(request: Request) -> JSONResponse:
        base = derive_base_url(request.headers.get("host", ""), cfg)
        return JSONResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": None,
                "token_endpoint": f"{base}/token",
                "grant_types_supported": ["client_credentials"],
                "response_types_supported": [],
                "token_endpoint_auth_methods_supported": [
                    "client_secret_basic",
                    "client_secret_post",
                ],
                "scopes_supported": SCOPES_SUPPORTED,
            }
        )

    async def oauth_protected_resource(request: Request) -> JSONResponse:
        base = derive_base_url(request.headers.get("host", ""), cfg)
        return JSONResponse(
            {
                "resource": f"{base}/mcp",
                "bearer_methods_supported": ["header"],
                "scopes_supported": SCOPES_SUPPORTED,
            }
        )

    async def token_endpoint(request: Request) -> JSONResponse:
        form = await request.form()

        if form.get("grant_type") != "client_credentials":
            return _json_error(
                request,
                "unsupported_grant_type",
                400,
                "only grant_type=client_credentials is supported",
            )

        client_id = None
        client_secret = None
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[6:]).decode("utf-8")
                client_id, client_secret = decoded.split(":", 1)
            except (ValueError, UnicodeDecodeError, binascii.Error):
                return _json_error(request, "invalid_client", 401, "malformed basic credentials")
        elif "client_id" in form:
            client_id = form.get("client_id")
            client_secret = form.get("client_secret")
        else:
            return _json_error(request, "invalid_client", 401, "client credentials required")

        client = clients_by_id.get(client_id or "")
        if client is None or not auth.verify_client_secret(client, client_secret or ""):
            return _json_error(request, "invalid_client", 401, "client authentication failed")

        requested_scopes = (form.get("scope") or "").split()
        if requested_scopes:
            granted = [scope for scope in client.scopes if scope in requested_scopes]
            if not granted:
                return _json_error(request, "invalid_scope", 400, "no requested scope is permitted")
        else:
            granted = list(client.scopes)

        token = auth.issue_token(client.client_id, granted, signing_key, issuer, cfg.auth.token_ttl_s)
        claims = auth.validate_token(token, signing_key, issuer)
        db.log_token_issue(
            claims["jti"],
            client.client_id,
            claims["scope"],
            _iso_epoch(claims["iat"]),
            _iso_epoch(claims["exp"]),
        )
        return JSONResponse(
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": cfg.auth.token_ttl_s,
                "scope": " ".join(granted),
            }
        )

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", oauth_authorization_server, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource, methods=["GET"]),
        Route("/token", token_endpoint, methods=["POST"]),
    ]

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with session_manager.run():
            yield

    plain_app = Starlette(routes=routes, lifespan=lifespan)
    return RequestLoggingMiddleware(GatewayRouter(plain_app, authed_mcp))
