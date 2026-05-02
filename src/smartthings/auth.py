"""SmartThings OAuth 2.0 — Authorization Code flow + token persistence.

Mirrors the shape of :mod:`src.daikin.auth`. Differences from Daikin:

  - Samsung uses **HTTP Basic Auth** for client credentials at the token
    endpoint (Daikin sends client_id/client_secret as form fields).
  - Token URL: ``https://auth-global.api.smartthings.com/oauth/token``
  - Authorize URL: ``https://api.smartthings.com/oauth/authorize``
  - Access token expires every 24 h; refresh_token has long-life (~30 days
    of inactivity is the published baseline).

Token file format (JSON, 0600):

    {
      "access_token":  "...",
      "refresh_token": "...",
      "expires_in":    86400,
      "token_type":    "bearer",
      "scope":         "r:devices:* x:devices:*",
      "obtained_at":   1714657123        # epoch seconds, set by us
    }

Bootstrap path (one-shot):

    docker compose -f deploy/compose.smartthings-auth.yaml run --rm smartthings-auth

The container starts a callback HTTP server on :8080, prints the
authorize URL, the operator opens it in their laptop browser (via
``ssh -L 8080:localhost:8080``), Samsung redirects back with ``?code=…``,
the server exchanges the code for tokens and writes the JSON file to
``data/.smartthings-tokens.json`` on the volume.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Event

from ..config import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors + circuit breaker (mirror Daikin)
# ---------------------------------------------------------------------------

class SmartThingsAuthError(RuntimeError):
    """Raised when the OAuth flow can't complete (bad code, refresh failure,
    misconfig, etc.). Distinct from :class:`SmartThingsError` which is for
    REST errors against the device API."""


class SmartThingsAuthCircuitOpen(RuntimeError):
    """Refresh circuit breaker tripped — refuse further refresh attempts
    until the cooldown elapses."""


_AUTH_CIRCUIT_THRESHOLD = 3
_AUTH_CIRCUIT_COOLDOWN_SECONDS = 900   # 15 min — prevents heartbeat hammering

_circuit_lock = threading.Lock()
_consecutive_auth_failures = 0
_circuit_tripped_at = 0.0
_token_io_lock = threading.RLock()
_last_token_refresh_monotonic = 0.0


def _auth_circuit_state() -> tuple[int, bool, float]:
    """Return (failures_count, tripped_now, cooldown_remaining_s)."""
    with _circuit_lock:
        now = time.monotonic()
        tripped = (
            _consecutive_auth_failures >= _AUTH_CIRCUIT_THRESHOLD
            and (now - _circuit_tripped_at) < _AUTH_CIRCUIT_COOLDOWN_SECONDS
        )
        remaining = max(
            0.0,
            _AUTH_CIRCUIT_COOLDOWN_SECONDS - (now - _circuit_tripped_at),
        ) if tripped else 0.0
        return _consecutive_auth_failures, tripped, remaining


def _reset_auth_circuit() -> None:
    global _consecutive_auth_failures, _circuit_tripped_at
    with _circuit_lock:
        _consecutive_auth_failures = 0
        _circuit_tripped_at = 0.0


def _record_auth_failure(reason: str) -> None:
    global _consecutive_auth_failures, _circuit_tripped_at
    with _circuit_lock:
        _consecutive_auth_failures += 1
        if _consecutive_auth_failures >= _AUTH_CIRCUIT_THRESHOLD:
            _circuit_tripped_at = time.monotonic()
            logger.error(
                "SmartThings auth circuit tripped (%d consecutive failures, "
                "last reason: %s) — refusing refresh for %ds",
                _consecutive_auth_failures, reason, _AUTH_CIRCUIT_COOLDOWN_SECONDS,
            )


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def _resolve_token_path() -> Path:
    p = Path(config.SMARTTHINGS_TOKEN_FILE)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


def save_tokens(tokens: dict) -> None:
    """Write the tokens dict to disk with 0600 perms (atomic via tmp+rename)."""
    p = _resolve_token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(p)


def load_tokens() -> dict:
    """Read the tokens dict from disk; raise SmartThingsAuthError if absent."""
    p = _resolve_token_path()
    if not p.is_file():
        raise SmartThingsAuthError(
            f"token file {p} not present — run the smartthings-auth container "
            "(docker compose -f deploy/compose.smartthings-auth.yaml run --rm smartthings-auth)"
        )
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SmartThingsAuthError(f"token file {p} unreadable: {e}") from e


def has_tokens() -> bool:
    return _resolve_token_path().is_file()


# ---------------------------------------------------------------------------
# Token endpoint (Basic Auth + form-encoded body)
# ---------------------------------------------------------------------------

def _basic_auth_header() -> str:
    """Build the ``Authorization: Basic <base64(client_id:client_secret)>`` header.

    Samsung requires Basic Auth at the token endpoint (Daikin uses form fields).
    """
    if not config.SMARTTHINGS_CLIENT_ID or not config.SMARTTHINGS_CLIENT_SECRET:
        raise SmartThingsAuthError(
            "SMARTTHINGS_CLIENT_ID / SMARTTHINGS_CLIENT_SECRET unset"
        )
    raw = f"{config.SMARTTHINGS_CLIENT_ID}:{config.SMARTTHINGS_CLIENT_SECRET}"
    b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return f"Basic {b64}"


def _post_token(form_fields: dict[str, str]) -> dict:
    """POST form-encoded fields to the SmartThings token endpoint.

    Uses curl because urllib's HTTPSConnection sometimes hits CloudFront WAFs
    that the Daikin endpoint also fronts (kept for symmetry — no observed
    issue on Samsung yet, but the same shape avoids a divergent bug surface).
    """
    body = urllib.parse.urlencode(form_fields)
    result = subprocess.run(
        [
            "curl", "-s", "-X", "POST", config.SMARTTHINGS_TOKEN_URL,
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "-H", "Accept: application/json",
            "-H", f"Authorization: {_basic_auth_header()}",
            "-d", body,
        ],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise SmartThingsAuthError(f"curl failed: {result.stderr}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise SmartThingsAuthError(
            f"token endpoint returned non-JSON: {result.stdout[:300]!r}"
        ) from e
    if "error" in payload:
        raise SmartThingsAuthError(
            f"{payload['error']}: {payload.get('error_description', '')}"
        )
    payload["obtained_at"] = int(time.time())
    return payload


def exchange_code(code: str) -> dict:
    """Exchange an authorization code for tokens (initial flow)."""
    return _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.SMARTTHINGS_REDIRECT_URI,
    })


def refresh_tokens(tokens: dict) -> dict:
    """Refresh the access token via refresh_token; gated by a circuit breaker.

    On success: writes new tokens to disk, clears any prior circuit fault.
    On failure: increments failure counter; trips the circuit after 3.
    """
    _, tripped, remaining = _auth_circuit_state()
    if tripped:
        raise SmartThingsAuthCircuitOpen(
            f"SmartThings auth circuit open (cooldown {remaining:.0f}s remaining) — re-auth required"
        )
    rt = tokens.get("refresh_token")
    if not rt:
        raise SmartThingsAuthError("token file has no refresh_token — re-enrollment required")
    try:
        new_tokens = _post_token({
            "grant_type": "refresh_token",
            "refresh_token": rt,
        })
    except SmartThingsAuthError as e:
        _record_auth_failure(str(e)[:80])
        raise
    # Samsung sometimes omits refresh_token on refresh; carry the previous one.
    if "refresh_token" not in new_tokens:
        new_tokens["refresh_token"] = rt
    save_tokens(new_tokens)
    _reset_auth_circuit()
    return new_tokens


def get_valid_access_token(*, force_refresh: bool = False) -> str:
    """Return an access token, refreshing if within leeway of expiry.

    Called by every SmartThings API request via the client; on a 401 the client
    can retry once with ``force_refresh=True`` to handle clock skew or revoked
    access tokens.
    """
    global _last_token_refresh_monotonic
    with _token_io_lock:
        tokens = load_tokens()
        exp = max(60.0, float(tokens.get("expires_in", 86400)))
        obtained_at = float(tokens.get("obtained_at", 0))
        leeway_cfg = max(60, int(config.SMARTTHINGS_ACCESS_REFRESH_LEEWAY_SECONDS))
        leeway = min(leeway_cfg, max(60, int(exp) - 30))
        stale = time.time() > obtained_at + exp - leeway
        hard_expired = time.time() > obtained_at + exp
        if force_refresh or stale:
            min_gap = max(0, int(config.SMARTTHINGS_TOKEN_REFRESH_MIN_INTERVAL_SECONDS))
            now_m = time.monotonic()
            if (
                not force_refresh
                and not hard_expired
                and min_gap > 0
                and (now_m - _last_token_refresh_monotonic) < min_gap
            ):
                return tokens["access_token"]
            tokens = refresh_tokens(tokens)
            _last_token_refresh_monotonic = time.monotonic()
        return tokens["access_token"]


# ---------------------------------------------------------------------------
# One-shot enrollment server (callback handler)
# ---------------------------------------------------------------------------

def authorize_url(state: str) -> str:
    """Build the Samsung OAuth consent URL the operator opens in a browser."""
    if not config.SMARTTHINGS_CLIENT_ID:
        raise SmartThingsAuthError("SMARTTHINGS_CLIENT_ID unset")
    qs = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": config.SMARTTHINGS_CLIENT_ID,
        "redirect_uri": config.SMARTTHINGS_REDIRECT_URI,
        "scope": config.SMARTTHINGS_OAUTH_SCOPES,
        "state": state,
    })
    return f"{config.SMARTTHINGS_AUTHORIZE_URL}?{qs}"


_auth_result: dict = {}
_done_event = Event()
_expected_state: str = ""


def _success_page(message: str = "Authentication successful") -> bytes:
    return (
        b"<!DOCTYPE html><html><head><title>SmartThings auth</title>"
        b"<meta charset=\"utf-8\"><style>body{font-family:sans-serif;display:flex;"
        b"justify-content:center;align-items:center;min-height:100vh;margin:0;"
        b"background:#f0f9ff;}.card{background:white;padding:2rem;border-radius:12px;"
        b"box-shadow:0 2px 12px rgba(0,0,0,0.08);text-align:center;}"
        b".ok{color:#16a34a;font-size:3rem;}</style></head>"
        b"<body><div class=\"card\"><div class=\"ok\">&#10003;</div>"
        b"<h2>" + message.encode("utf-8") + b"</h2>"
        b"<p>You can close this tab.</p></div></body></html>"
    )


def _error_page(reason: str) -> bytes:
    return (
        b"<!DOCTYPE html><html><head><title>SmartThings auth - error</title>"
        b"<meta charset=\"utf-8\"></head><body style=\"font-family:sans-serif;"
        b"max-width:560px;margin:40px auto;padding:0 20px;\">"
        b"<h2 style=\"color:#b91c1c;\">Authentication failed</h2>"
        b"<p>" + reason.encode("utf-8") + b"</p></body></html>"
    )


class _CallbackHandler(BaseHTTPRequestHandler):
    """Handle ``GET /oauth/smartthings/callback?code=…&state=…``.

    Validates the state token, exchanges the code via :func:`exchange_code`,
    persists the tokens, signals completion, and returns a success/error page.
    """

    def do_GET(self) -> None:  # noqa: N802 — required by BaseHTTPRequestHandler
        global _auth_result
        url = urllib.parse.urlparse(self.path)
        if not url.path.endswith("/oauth/smartthings/callback"):
            self.send_response(404)
            self.end_headers()
            return
        params = dict(urllib.parse.parse_qsl(url.query))
        if params.get("error"):
            reason = (
                f"{params['error']}: "
                f"{params.get('error_description', '')}"
            )
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_error_page(reason))
            _auth_result = {"error": reason}
            _done_event.set()
            return
        if params.get("state") != _expected_state:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_error_page("state mismatch — possible CSRF; restart the auth flow"))
            _auth_result = {"error": "state_mismatch"}
            _done_event.set()
            return
        code = params.get("code")
        if not code:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_error_page("no ?code= parameter in callback"))
            _auth_result = {"error": "no_code"}
            _done_event.set()
            return
        try:
            tokens = exchange_code(code)
            save_tokens(tokens)
        except Exception as e:  # noqa: BLE001
            self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_error_page(f"token exchange failed: {e}"))
            _auth_result = {"error": str(e)}
            _done_event.set()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_success_page())
        _auth_result = {"ok": True, "scope": tokens.get("scope")}
        _done_event.set()

    def log_message(self, _fmt, *_args):  # noqa: N802 — interface override
        # Suppress default stderr access log; routes our own info via logger
        # if/when needed.
        pass


def run_auth_flow(*, port: int | None = None, timeout_s: int = 600) -> dict:
    """Spawn the callback server, print the authorize URL, wait for the redirect.

    Returns the result dict (``{"ok": True, ...}`` on success or
    ``{"error": "..."}`` on failure). Times out after ``timeout_s``.
    """
    global _auth_result, _expected_state
    _auth_result = {}
    _done_event.clear()
    _expected_state = secrets.token_urlsafe(16)

    # Bind port. Default = 8080 to match the Daikin pattern + our redirect URI.
    bind_port = port if port is not None else 8080
    server = HTTPServer(("0.0.0.0", bind_port), _CallbackHandler)  # noqa: S104
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = authorize_url(_expected_state)
        print("\n" + "=" * 70)
        print("Open this URL in your browser (with SSH tunnel to localhost:%d):" % bind_port)
        print("\n  " + url + "\n")
        print("Waiting for the callback... (timeout %ds)" % timeout_s)
        print("=" * 70 + "\n")
        if not _done_event.wait(timeout=timeout_s):
            return {"error": "timeout waiting for callback"}
        return dict(_auth_result)
    finally:
        server.shutdown()
        thread.join(timeout=5)


def run_setup() -> dict | None:
    """Top-level entry for the ``smartthings-auth`` one-shot container."""
    result = run_auth_flow()
    if result.get("ok"):
        path = _resolve_token_path()
        print(f"\n[OK] Tokens written to {path}\n")
        print(f"     Scopes granted: {result.get('scope')}\n")
        return result
    err = result.get("error", "unknown")
    print(f"\n[FAIL] {err}\n")
    return None


# Convenience for prefetching at heartbeat (called by scheduler if wired)
def prefetch_smartthings_access_token() -> None:
    if not config.SMARTTHINGS_CLIENT_ID or not config.SMARTTHINGS_CLIENT_SECRET:
        return
    if not has_tokens():
        return
    try:
        get_valid_access_token(force_refresh=False)
    except (SmartThingsAuthError, SmartThingsAuthCircuitOpen):
        # Quiet — on-the-wire errors will be handled by the caller's notify_risk.
        pass
