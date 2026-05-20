"""ASGI middleware — bearer-token guards for MCP and /api/v1.

Two middlewares live here:

* :class:`BearerAuthMiddleware` — wraps the ``/mcp`` mount with a single
  expected token. The lifespan in :mod:`src.api.main` bootstraps the
  token file on first boot.
* :class:`ApiV1BearerAuth` — gates ``/api/v1/*`` requests by bearer
  token. Accepts EITHER ``HEM_UI_TOKEN`` (for the UI container in
  Epic 13b) OR ``HEM_OPENCLAW_TOKEN`` (so the existing OpenClaw flows
  keep working through a single header). The guard is gated by
  ``HEM_UI_AUTH_REQUIRED`` so B1 can ship + reach prod without
  breaking the inline UI before B6's cutover — flip the flag once
  the SPA container is live.

Both middlewares accept ``TokenLike`` — either a string or a zero-arg
callable — so callers can pass ``lambda: config.HEM_*_TOKEN`` and see
the live token at request time rather than at import time.
"""
from __future__ import annotations

import hmac
from typing import Callable, Iterable, Union

from starlette.types import ASGIApp, Receive, Scope, Send

TokenLike = Union[str, Callable[[], str]]


def _resolve(t: TokenLike) -> str:
    return (t() if callable(t) else t or "").strip()


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


class ApiV1BearerAuth:
    """Bearer guard for ``/api/v1/*`` — gated by ``enabled()`` so the
    middleware can sit on the chain in non-enforcing mode until the SPA
    container is the only consumer.

    Three call-time inputs:

    * ``tokens`` — iterable of ``TokenLike``; any non-empty match passes.
      Pass ``[lambda: config.HEM_UI_TOKEN, lambda: config.HEM_OPENCLAW_TOKEN]``.
    * ``enabled`` — zero-arg callable returning bool. When False, the
      middleware is a no-op — requests pass through with no header check.
    * ``public_paths`` — iterable of exact paths skipped even when
      enabled. Defaults to ``{"/api/v1/health"}`` so compose's
      ``healthcheck:`` keeps working unauthenticated.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        tokens: Iterable[TokenLike],
        enabled: Callable[[], bool],
        prefix: str = "/api/v1/",
        public_paths: Iterable[str] = ("/api/v1/health",),
    ) -> None:
        self.app = app
        self._tokens = list(tokens)
        self._enabled = enabled
        self._prefix = prefix
        self._public_paths = frozenset(public_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith(self._prefix) or path in self._public_paths:
            await self.app(scope, receive, send)
            return

        if not self._enabled():
            await self.app(scope, receive, send)
            return

        # Resolve expected tokens lazily so a token rotated in config at
        # runtime is honoured without a restart.
        expected_tokens = [t for t in (_resolve(t) for t in self._tokens) if t]
        if not expected_tokens:
            await _reject(send, 503, "service token not provisioned")
            return

        presented = ""
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                presented = value.decode("latin-1", errors="replace")
                break

        if not presented.lower().startswith("bearer "):
            await _reject(send, 401, "missing bearer token", realm="hem-api")
            return

        offered = presented[7:].strip().encode("utf-8")
        if not any(
            hmac.compare_digest(offered, exp.encode("utf-8"))
            for exp in expected_tokens
        ):
            await _reject(send, 401, "invalid token", realm="hem-api")
            return

        await self.app(scope, receive, send)


async def _reject(send: Send, status: int, message: str, *, realm: str = "hem-mcp") -> None:
    body = b'{"error":"' + message.encode("utf-8") + b'"}'
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", f'Bearer realm="{realm}"'.encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
