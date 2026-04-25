"""ASGI middleware — bearer-token guard for the MCP HTTP transport mount.

The HEM exposes ``streamable_http_app()`` from FastMCP under ``/mcp`` for
OpenClaw to call tools over HTTP (replacing the legacy stdio subprocess
launch). That mount must be authenticated; this middleware does it without
pulling Starlette response classes into a hot path.

Token resolution is deferred — the middleware accepts either a string or a
zero-arg callable. The lifespan in :mod:`src.api.main` bootstraps the token
file on first boot and sets ``config.HEM_OPENCLAW_TOKEN`` so the callable
form below sees the live value at request time.
"""
from __future__ import annotations

import hmac
from typing import Callable, Union

from starlette.types import ASGIApp, Receive, Scope, Send

TokenLike = Union[str, Callable[[], str]]


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, *, token: TokenLike) -> None:
        self.app = app
        self._token: Callable[[], str] = token if callable(token) else (lambda t=token: t)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        expected = (self._token() or "").strip()
        if not expected:
            await _reject(send, 503, "service token not provisioned")
            return

        presented = ""
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                presented = value.decode("latin-1", errors="replace")
                break

        if not presented.lower().startswith("bearer "):
            await _reject(send, 401, "missing bearer token")
            return

        if not hmac.compare_digest(presented[7:].strip().encode("utf-8"), expected.encode("utf-8")):
            await _reject(send, 401, "invalid token")
            return

        await self.app(scope, receive, send)


async def _reject(send: Send, status: int, message: str) -> None:
    body = b'{"error":"' + message.encode("utf-8") + b'"}'
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", b'Bearer realm="hem-mcp"'),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
