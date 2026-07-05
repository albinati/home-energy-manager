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

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _resolve(t: TokenLike) -> str:
    return (t() if callable(t) else t or "").strip()


def token_matches_any(presented: str, tokens: Iterable[TokenLike]) -> bool:
    """Constant-time check: does ``presented`` equal any non-empty token?

    Shared by :class:`ApiV1RoleAuth` and the ``/whoami`` handler so the
    definition of "is this an admin token" lives in exactly one place.
    """
    offered = (presented or "").strip().encode("utf-8")
    if not offered:
        return False
    matched = False
    for t in tokens:
        exp = _resolve(t)
        if exp and hmac.compare_digest(offered, exp.encode("utf-8")):
            matched = True  # keep looping — constant-ish, don't early-return
    return matched


def bearer_from_scope(scope: Scope) -> str:
    """Extract the bearer token value from an ASGI scope's headers (or "")."""
    for name, value in scope.get("headers") or []:
        if name == b"authorization":
            presented = value.decode("latin-1", errors="replace")
            if presented.lower().startswith("bearer "):
                return presented[7:].strip()
            return ""
    return ""


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


class ApiV1RoleAuth:
    """Role-based guard for ``/api/v1/*`` — viewer-open, admin-gated.

    The system is shareable as a passive **viewer** (read-only) without any
    token, while **admin** actions (anything that mutates state, plus the
    Settings and Journal admin reads) require an admin token.

    Per request (when ``enabled()``):

    * **Public paths** (``/health``, ``/whoami``) — always pass.
    * **Safe reads** — ``GET``/``HEAD``/``OPTIONS`` on a non-admin path pass for
      everyone, with or without a token (the viewer surface).
    * **Admin-required** — any non-safe method (POST/PUT/PATCH/DELETE) OR a
      path under ``admin_read_prefixes`` (Settings, Journal/action-log,
      integration credentials) — pass only with a valid admin token, else 401.

    When ``enabled()`` is False the middleware is a no-op (dev: all open).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        admin_tokens: Iterable[TokenLike],
        enabled: Callable[[], bool],
        prefix: str = "/api/v1/",
        public_paths: Iterable[str] = ("/api/v1/health", "/api/v1/whoami"),
        admin_read_prefixes: Iterable[str] = (
            "/api/v1/settings",
            # Journal/action-log is served under several paths — gate the DATA,
            # not just one route (a security review caught /schedule/history and
            # /recent-triggers leaking the same action_log a viewer must not see).
            "/api/v1/action-log",
            "/api/v1/schedule/history",
            "/api/v1/recent-triggers",
            "/api/v1/integrations",   # SmartThings credentials / OAuth
            "/api/v1/workbench",      # LP override sandbox (admin tool, in Settings)
        ),
        ingest_tokens: Iterable[TokenLike] = (),
        ingest_write_prefixes: Iterable[str] = ("/api/v1/sensors/indoor",),
    ) -> None:
        self.app = app
        self._admin_tokens = list(admin_tokens)
        self._enabled = enabled
        self._prefix = prefix
        self._public_paths = frozenset(public_paths)
        self._admin_read_prefixes = tuple(admin_read_prefixes)
        # Scoped, non-admin write credential(s). A match satisfies ONLY a write
        # (non-safe method) to a whitelisted ingest prefix — never an admin read
        # or any other write. This is the token an internet-exposed device (e.g.
        # an ESPHome room sensor pushing to /sensors/indoor via a locked-down
        # proxy) carries, so a firmware leak can't hand over admin.
        self._ingest_tokens = list(ingest_tokens)
        self._ingest_write_prefixes = tuple(ingest_write_prefixes)

    def _needs_admin(self, method: str, path: str) -> bool:
        if method.upper() not in SAFE_METHODS:
            return True
        return any(path.startswith(p) for p in self._admin_read_prefixes)

    def _ingest_allowed(self, method: str, path: str) -> bool:
        """True only for a *write* to a whitelisted ingest route — the sole
        surface a scoped ingest token unlocks. A safe method (GET on an
        admin_read_prefix) or any non-ingest write returns False, so the
        scoped token is useless everywhere except its one endpoint."""
        if method.upper() in SAFE_METHODS:
            return False
        return any(path.startswith(p) for p in self._ingest_write_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith(self._prefix):
            await self.app(scope, receive, send)
            return

        if not self._enabled():
            await self.app(scope, receive, send)
            return

        if path in self._public_paths:
            await self.app(scope, receive, send)
            return

        if not self._needs_admin(scope.get("method", "GET"), path):
            await self.app(scope, receive, send)  # viewer-permissible read
            return

        # Admin required from here on.
        presented = bearer_from_scope(scope)
        if not presented:
            await _reject(send, 401, "admin token required", realm="hem-api")
            return
        if token_matches_any(presented, self._admin_tokens):
            await self.app(scope, receive, send)
            return
        # A scoped ingest token unlocks its whitelisted write routes ONLY.
        if (
            self._ingest_tokens
            and self._ingest_allowed(scope.get("method", "GET"), path)
            and token_matches_any(presented, self._ingest_tokens)
        ):
            await self.app(scope, receive, send)
            return
        await _reject(send, 401, "invalid admin token", realm="hem-api")
        return


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
