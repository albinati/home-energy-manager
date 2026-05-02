"""Daikin Onecta OAuth2 auth flow.

Modes:
    python -m src.daikin.auth             # normal auth (server already registered)
    python -m src.daikin.auth --setup     # full setup: tunnel + app registration + auth
    python -m src.daikin.auth --code CODE # exchange a code manually

The --setup mode creates a public tunnel via localhost.run so that Daikin's
developer portal can ping the redirect URI during app registration (the
"registration hook").  It then walks you through creating the app, entering
credentials, and completing the OAuth flow — all in one go.

Developer portal: https://developer.cloud.daikineurope.com
"""
import html
import json
import logging
import re
import subprocess
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Event, RLock
from urllib.parse import parse_qs, urlparse

from ..config import config

logger = logging.getLogger(__name__)

TOKEN_FILE = config.DAIKIN_TOKEN_FILE

# Serialize token file I/O + refresh when API and heartbeat overlap.
_token_io_lock = RLock()
_last_token_refresh_monotonic: float = 0.0

# Circuit breaker for dead refresh tokens. If refresh_tokens() fails with
# an auth error repeatedly (refresh_token expired, client credentials
# revoked, etc.) we'd otherwise hammer the Onecta token endpoint on every
# heartbeat. The breaker tracks consecutive failures and, once a threshold
# is crossed, refuses to retry until the cool-down window passes — freeing
# the service to keep running on the physics estimator without a storm of
# noisy 401/400 logs and without burning the 200/day quota on doomed auth
# attempts.
_consecutive_auth_failures: int = 0
_circuit_tripped_at_monotonic: float = 0.0
_circuit_notified: bool = False


class DaikinAuthCircuitOpen(RuntimeError):
    """Raised by refresh_tokens() when the circuit is tripped.

    Callers (DaikinClient._get / _patch) should treat this like a 401
    surfaced to the user — the service falls back to cached data / the
    physics estimator. A critical notification is emitted once per trip
    so the user knows a manual re-auth is needed.
    """

_auth_result = {}
_done_event = Event()
_hook_received = Event()


def _exchange_code(code: str) -> dict:
    """Exchange authorization code for tokens via curl (bypasses CloudFront WAF)."""
    post_data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.DAIKIN_REDIRECT_URI,
        "client_id": config.DAIKIN_CLIENT_ID,
        "client_secret": config.DAIKIN_CLIENT_SECRET,
    })
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", config.DAIKIN_TOKEN_URL,
         "-H", "Content-Type: application/x-www-form-urlencoded",
         "-H", "User-Agent: Mozilla/5.0",
         "-d", post_data],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr}")
    tokens = json.loads(result.stdout)
    if "error" in tokens:
        raise RuntimeError(f"{tokens['error']}: {tokens.get('error_description', '')}")
    tokens["obtained_at"] = int(time.time())
    return tokens


def _success_page() -> bytes:
    return b"""<!DOCTYPE html>
<html><head><title>Daikin Auth</title>
<meta charset="utf-8">
<style>body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#f0f9ff;}
.card{background:white;padding:2rem;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,0.08);text-align:center;}
.ok{color:#16a34a;font-size:3rem;}</style></head>
<body><div class="card"><div class="ok">&#10003;</div><h2>Authentication successful</h2><p>You can close this tab.</p></div></body></html>"""


def _code_fallback_page(code: str, err: str) -> bytes:
    return f"""<!DOCTYPE html>
<html><head><title>Daikin Auth — use code</title>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 560px; margin: 40px auto; padding: 0 20px; }}
  .box {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; font-family: monospace; word-break: break-all; }}
  code {{ font-size: 14px; }}
  .err {{ color: #b91c1c; font-size: 14px; margin-bottom: 16px; }}
  ol {{ line-height: 1.8; }}
</style></head>
<body>
  <h1>Token exchange from this server failed</h1>
  <p class="err"><b>Reason:</b> {html.escape(err)}</p>
  <p>Run the exchange from your PC (PowerShell or CMD) so the request goes from your network:</p>
  <ol>
    <li>Copy the authorization code below.</li>
    <li>In the project folder, run:<br>
      <span class="box"><code>python -m src.daikin.auth --code PASTE_CODE_HERE</code></span></li>
  </ol>
  <p><b>Authorization code (copy this):</b></p>
  <div class="box"><code>{html.escape(code)}</code></div>
  <p><small>This code is one-time use and expires in a few minutes.</small></p>
</body></html>""".encode()


EXCHANGE_PAGE = """<!DOCTYPE html>
<html><head><title>Daikin Auth</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         display: flex; justify-content: center; align-items: center;
         min-height: 100vh; margin: 0; background: #f5f5f5; }}
  .card {{ background: white; border-radius: 12px; padding: 40px;
           box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 480px;
           text-align: center; }}
  .spinner {{ border: 3px solid #eee; border-top: 3px solid #0078d4;
              border-radius: 50%; width: 32px; height: 32px;
              animation: spin 1s linear infinite; margin: 16px auto; }}
  @keyframes spin {{ 0% {{ transform: rotate(0deg); }}
                     100% {{ transform: rotate(360deg); }} }}
  .ok {{ color: #2e7d32; font-size: 48px; }}
  .err {{ color: #c62828; }}
</style></head>
<body><div class="card" id="card">
  <div class="spinner" id="spinner"></div>
  <p id="msg">Exchanging authorization code for tokens...</p>
</div>
<script>
async function exchange() {{
  try {{
    const resp = await fetch("{token_url}", {{
      method: "POST",
      headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
      body: new URLSearchParams({{
        grant_type: "authorization_code",
        code: "{code}",
        redirect_uri: "{redirect_uri}",
        client_id: "{client_id}",
        client_secret: "{client_secret}"
      }})
    }});
    const tokens = await resp.json();
    if (tokens.access_token) {{
      // Send tokens to local server
      await fetch("/save-tokens", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(tokens)
      }});
      document.getElementById("spinner").style.display = "none";
      document.getElementById("msg").innerHTML =
        '<div class="ok">&#10003;</div>' +
        '<h2>Authentication successful!</h2>' +
        '<p>You can close this tab and return to the terminal.</p>';
    }} else {{
      throw new Error(tokens.error_description || tokens.error || JSON.stringify(tokens));
    }}
  }} catch(e) {{
    document.getElementById("spinner").style.display = "none";
    document.getElementById("msg").innerHTML =
      '<p class="err"><b>Error:</b> ' + e.message + '</p>' +
      '<p>Check the terminal for details.</p>';
    // Report error to server
    await fetch("/save-tokens", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ error: e.message }})
    }});
  }}
}}
exchange();
</script></body></html>"""


class CallbackHandler(BaseHTTPRequestHandler):
    """Handles OAuth callbacks AND registration-hook pings from the Daikin portal."""

    def _ok_json(self, body: bytes = b'{"status":"ok"}'):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Daikin Auth Server</h1><p>Running.</p>")
            return

        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)

        # --- Registration hook ping (no OAuth params) -----------------
        if "code" not in params and "error" not in params:
            print("  [hook] Registration ping received — responded 200 OK")
            _hook_received.set()
            self._ok_json()
            return

        # --- OAuth error ----------------------------------------------
        if "error" in params:
            err = params.get("error_description", params["error"])[0]
            _auth_result["error"] = err
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<h1>Auth error</h1><p>{html.escape(err)}</p>".encode())
            _done_event.set()
            return

        # --- OAuth callback with code ---------------------------------
        code = params.get("code", [None])[0]
        if not code:
            _auth_result["error"] = "No code in callback"
            self.send_response(400)
            self.end_headers()
            _done_event.set()
            return

        exchange_err = None
        try:
            tokens = _exchange_code(code)
            _auth_result["tokens"] = tokens
            page = _success_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(page if isinstance(page, bytes) else page.encode())
            _done_event.set()
            return
        except Exception as exc:
            exchange_err = str(exc)

        page = _code_fallback_page(code, exchange_err)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(page if isinstance(page, bytes) else page.encode())

    # ------------------------------------------------------------------
    def do_POST(self):
        # Registration hook may also POST
        if self.path == "/callback" or self.path == "/":
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)  # drain body
            print("  [hook] Registration POST received — responded 200 OK")
            _hook_received.set()
            self._ok_json()
            return

        if self.path == "/save-tokens":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            if "error" in body:
                _auth_result["error"] = body["error"]
            else:
                _auth_result["tokens"] = body
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            _done_event.set()
            return

        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, _fmt, *_args):
        # Override BaseHTTPRequestHandler.log_message to suppress the
        # default stderr access log; signature must match the interface.
        pass


def run_auth_flow() -> dict:
    """Run the OAuth2 authorisation code flow with a local callback server."""
    import secrets
    state = secrets.token_urlsafe(16)

    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": config.DAIKIN_CLIENT_ID,
        "redirect_uri": config.DAIKIN_REDIRECT_URI,
        "scope": "openid onecta:basic.integration",
        "state": state,
    })
    auth_url = f"{config.DAIKIN_AUTH_URL}?{params}"

    server = HTTPServer(("0.0.0.0", 8080), CallbackHandler)
    server.timeout = 1

    # Wrap with SSL if redirect URI uses https
    if config.DAIKIN_REDIRECT_URI.startswith("https://"):
        import ssl
        cert_dir = Path(__file__).resolve().parent.parent.parent
        certfile = cert_dir / ".lvh-cert.pem"
        keyfile = cert_dir / ".lvh-key.pem"
        if not certfile.exists():
            raise FileNotFoundError(
                f"SSL cert not found: {certfile}\n"
                "Generate with: openssl req -x509 -newkey rsa:2048 -keyout .lvh-key.pem "
                "-out .lvh-cert.pem -days 365 -nodes -subj '/CN=lvh.me'"
            )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

    proto = "https" if config.DAIKIN_REDIRECT_URI.startswith("https://") else "http"
    print("\n" + "=" * 60)
    print("DAIKIN ONECTA — AUTHENTICATION")
    print("=" * 60)
    print(f"\nLocal callback server running on {proto}://lvh.me:8080")
    print("\nOpening browser — log in with your Daikin/Onecta account.")
    print(f"\nIf the browser doesn't open, visit this URL manually:\n{auth_url}\n")
    print("Waiting for callback... (Ctrl+C to cancel)\n")
    webbrowser.open(auth_url)

    try:
        while not _done_event.is_set():
            server.handle_request()
    except KeyboardInterrupt:
        print("\nCancelled.")
        server.server_close()
        raise SystemExit(1)

    server.server_close()

    if "error" in _auth_result:
        raise RuntimeError(f"Auth failed: {_auth_result['error']}")

    tokens = _auth_result["tokens"]
    tokens["obtained_at"] = int(time.time())
    save_tokens(tokens)

    print(f"✅ Authentication successful! Tokens saved to {TOKEN_FILE}")
    print("You can now run: python -m src.cli status\n")
    return tokens


def save_tokens(tokens: dict) -> None:
    """Persist tokens; uses temp file + replace to avoid torn writes."""
    with _token_io_lock:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(tokens, indent=2)
        tmp = TOKEN_FILE.with_suffix(TOKEN_FILE.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(TOKEN_FILE)


def load_tokens() -> dict:
    with _token_io_lock:
        if not TOKEN_FILE.exists():
            raise FileNotFoundError(
                f"Token file not found: {TOKEN_FILE}\n"
                "Run: python -m src.daikin.auth"
            )
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))


def _auth_circuit_state() -> tuple[int, bool, float]:
    """Snapshot of the auth circuit breaker (for tests / status endpoints)."""
    cooldown = max(60, int(getattr(config, "DAIKIN_AUTH_CIRCUIT_COOLDOWN_SECONDS", 900)))
    now = time.monotonic()
    remaining = max(0.0, _circuit_tripped_at_monotonic + cooldown - now) if _circuit_tripped_at_monotonic else 0.0
    return _consecutive_auth_failures, _circuit_tripped_at_monotonic > 0.0 and remaining > 0.0, remaining


def _reset_auth_circuit() -> None:
    """Called on a successful refresh — clears fault counter + cooldown."""
    global _consecutive_auth_failures, _circuit_tripped_at_monotonic, _circuit_notified
    if _consecutive_auth_failures or _circuit_tripped_at_monotonic:
        logger.info("Daikin auth circuit: cleared after successful refresh")
    _consecutive_auth_failures = 0
    _circuit_tripped_at_monotonic = 0.0
    _circuit_notified = False


def _record_auth_failure(reason: str) -> None:
    """Increment the fault counter and, on threshold, trip the circuit.

    Emits a critical notification exactly once per trip so the user sees
    the "refresh_token expired — re-auth required" alert without spam.
    """
    global _consecutive_auth_failures, _circuit_tripped_at_monotonic, _circuit_notified
    _consecutive_auth_failures += 1
    threshold = max(1, int(getattr(config, "DAIKIN_AUTH_CIRCUIT_THRESHOLD", 3)))
    if _consecutive_auth_failures >= threshold and _circuit_tripped_at_monotonic == 0.0:
        _circuit_tripped_at_monotonic = time.monotonic()
        logger.error(
            "Daikin auth circuit TRIPPED after %d consecutive failures (reason: %s) — "
            "skipping token refresh attempts for DAIKIN_AUTH_CIRCUIT_COOLDOWN_SECONDS",
            _consecutive_auth_failures,
            reason,
        )
        if not _circuit_notified:
            _circuit_notified = True
            try:
                # Import here to avoid a hard notifier dependency at module load.
                from ..notifier import notify_risk
                notify_risk(
                    "Daikin auth failing repeatedly — refresh_token likely expired. "
                    "Run `python -m src.daikin.auth` to re-authorise. Falling back "
                    "to cached/estimated telemetry in the meantime.",
                    extra={"consecutive_failures": _consecutive_auth_failures, "reason": reason},
                )
            except Exception:
                logger.debug("notifier unavailable for circuit-trip alert", exc_info=True)


def refresh_tokens(tokens: dict) -> dict:
    """Refresh access token using refresh token via curl.

    Wrapped by a circuit breaker: after
    DAIKIN_AUTH_CIRCUIT_THRESHOLD (default 3) consecutive failures, we
    refuse further refresh attempts for DAIKIN_AUTH_CIRCUIT_COOLDOWN_SECONDS
    (default 900 = 15 min). This prevents a dead refresh_token from
    hammering the Onecta token endpoint on every heartbeat and surfaces a
    single user-visible alert via notify_risk. The circuit clears on any
    successful refresh.
    """
    # Circuit gate: if already tripped and still within cooldown, fail fast.
    _, tripped, remaining = _auth_circuit_state()
    if tripped:
        raise DaikinAuthCircuitOpen(
            f"Daikin auth circuit open (cooldown {remaining:.0f}s remaining) — re-auth required"
        )

    post_data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": config.DAIKIN_CLIENT_ID,
        "client_secret": config.DAIKIN_CLIENT_SECRET,
    })
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", config.DAIKIN_TOKEN_URL,
         "-H", "Content-Type: application/x-www-form-urlencoded",
         "-H", "User-Agent: Mozilla/5.0",
         "-d", post_data],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        _record_auth_failure(f"curl rc={result.returncode}")
        raise RuntimeError(f"curl failed: {result.stderr}")
    try:
        new_tokens = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        _record_auth_failure(f"json parse: {e}")
        raise
    if "error" in new_tokens:
        _record_auth_failure(f"{new_tokens['error']}")
        raise RuntimeError(f"{new_tokens['error']}: {new_tokens.get('error_description', '')}")
    new_tokens["obtained_at"] = int(time.time())
    if "refresh_token" not in new_tokens:
        new_tokens["refresh_token"] = tokens["refresh_token"]
    save_tokens(new_tokens)
    # Success — clear any prior fault state.
    _reset_auth_circuit()
    return new_tokens


def get_valid_access_token(*, force_refresh: bool = False) -> str:
    """Return a valid access token, refreshing via refresh_token when needed.

    Called on every Daikin HTTP request. Tokens are written back to
    ``DAIKIN_TOKEN_FILE`` after refresh — use a persistent path in Docker (see
    ``docker-compose.yml``) so new refresh/access pairs survive restarts.

    If the API returns 401 (e.g. clock skew or revoked access token), callers
    should retry once with ``force_refresh=True``.
    """
    global _last_token_refresh_monotonic
    with _token_io_lock:
        tokens = load_tokens()
        exp = max(30.0, float(tokens.get("expires_in", 3600)))
        obtained_at = float(tokens.get("obtained_at", 0))
        leeway_cfg = max(60, int(config.DAIKIN_ACCESS_REFRESH_LEEWAY_SECONDS))
        # Cap leeway so we never treat "refresh early" as "always stale" on short-lived tokens.
        leeway = min(leeway_cfg, max(60, int(exp) - 30))
        stale = time.time() > obtained_at + exp - leeway
        hard_expired = time.time() > obtained_at + exp
        if force_refresh or stale:
            min_gap = max(0, int(config.DAIKIN_TOKEN_REFRESH_MIN_INTERVAL_SECONDS))
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


def prefetch_daikin_access_token() -> None:
    """Refresh the access token if it is within the configured leeway of expiry.

    Hits only the OIDC token endpoint (not the device API). Safe to call on a
    timer or at the start of the bulletproof heartbeat.
    """
    if not config.DAIKIN_CLIENT_ID or not config.DAIKIN_CLIENT_SECRET:
        return
    get_valid_access_token(force_refresh=False)


def run_auth_flow_with_code(code: str) -> dict:
    """Exchange a pasted authorization code for tokens (e.g. from Windows)."""
    code = code.strip()
    if not code:
        raise SystemExit("Empty code. Get the code from the callback URL after logging in.")
    print("Exchanging code for tokens...")
    tokens = _exchange_code(code)
    save_tokens(tokens)
    print(f"✅ Authentication successful! Tokens saved to {TOKEN_FILE}")
    print("You can now run: python -m src.cli status")
    return tokens


# ──────────────────────────────────────────────────────────────────────
#  Setup mode — public tunnel + app registration + auth in one go
# ──────────────────────────────────────────────────────────────────────

def _try_ssh_tunnel(local_port: int = 8080) -> tuple:
    """Start a public HTTPS tunnel to *local_port* via localhost.run (SSH).

    Returns ``(Popen, public_url)`` on success or ``(None, None)`` on failure.
    """
    print("Creating public tunnel via localhost.run (SSH — no install needed)…")
    try:
        proc = subprocess.Popen(
            [
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "ServerAliveInterval=30",
                "-o", "ConnectTimeout=10",
                "-R", f"80:localhost:{local_port}",
                "nokey@localhost.run",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        print("  ssh not found — tunnel unavailable.")
        return None, None

    start = time.time()
    url = None
    while time.time() - start < 20:
        line = proc.stdout.readline()
        if not line:
            break
        m = re.search(r"(https://\S+\.lhr\.life)", line)
        if m:
            url = m.group(1)
            break

    if not url:
        proc.kill()
        print("  Could not establish tunnel (timed out).")
        return None, None

    return proc, url


def _update_env(key: str, value: str) -> None:
    """Set *key*=*value* in the project .env file (create if missing)."""
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n")
        return
    lines = env_path.read_text().splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")


def run_setup() -> dict | None:
    """Full guided setup: tunnel → app registration → OAuth flow."""
    import secrets

    print("\n" + "=" * 60)
    print("  DAIKIN ONECTA — FULL SETUP")
    print("=" * 60)

    # 1. Start local HTTP server (plain — tunnel provides TLS)
    server = HTTPServer(("0.0.0.0", 8080), CallbackHandler)
    server.timeout = 1

    # 2. Start tunnel
    tunnel_proc, public_url = _try_ssh_tunnel(8080)

    if public_url:
        callback_url = f"{public_url}/callback"
        print("\n✅ Public tunnel ready!")
        print(f"   Redirect URI: {callback_url}\n")
    else:
        print("\n⚠️  SSH tunnel failed. Alternatives:")
        print("   • Install ngrok  → ngrok http 8080")
        print("   • Cloudflare     → cloudflared tunnel --url http://localhost:8080")
        print("   Then enter the public HTTPS URL below.\n")
        raw = input("Public HTTPS base URL (e.g. https://abc.ngrok-free.app): ").strip()
        callback_url = raw.rstrip("/") + "/callback"
        tunnel_proc = None

    # 3. Guide the user through portal registration
    print("-" * 60)
    print("STEP 1  Register a new Daikin app")
    print("-" * 60)
    print("\n1. Open  https://developer.cloud.daikineurope.com")
    print("2. Delete any old app (e.g. 'OpenClaw') if it exists.")
    print("3. Create a NEW app.  Set the redirect URI to:\n")
    print(f"       {callback_url}\n")
    print("4. Copy the Client ID and Client Secret (shown only once!).")
    print(f"\nThe server is running — Daikin will ping {callback_url}")
    print("and get a 200 OK (registration hook).\n")

    # Keep serving while the user registers the app
    print("Waiting for registration hook and your credentials…")
    print("(The server keeps running in the background.)\n")

    client_id = input("Paste Client ID: ").strip()
    client_secret = input("Paste Client Secret: ").strip()

    if not client_id or not client_secret:
        print("Client ID and Secret are both required. Aborting.")
        if tunnel_proc:
            tunnel_proc.kill()
        server.server_close()
        return None

    # 4. Persist credentials
    _update_env("DAIKIN_CLIENT_ID", client_id)
    _update_env("DAIKIN_CLIENT_SECRET", client_secret)
    _update_env("DAIKIN_REDIRECT_URI", callback_url)

    # Hot-patch running config so the rest of the flow uses new values
    config.DAIKIN_CLIENT_ID = client_id
    config.DAIKIN_CLIENT_SECRET = client_secret
    config.DAIKIN_REDIRECT_URI = callback_url

    print("\n✅ Credentials saved to .env")

    # 5. Start OAuth flow
    print("\n" + "-" * 60)
    print("STEP 2  Authenticate with Daikin")
    print("-" * 60)

    state = secrets.token_urlsafe(16)
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": callback_url,
        "scope": "openid onecta:basic.integration",
        "state": state,
    })
    auth_url = f"{config.DAIKIN_AUTH_URL}?{params}"

    print("\nOpening browser — log in with your Daikin/Onecta account.")
    print(f"If the browser doesn't open, visit:\n{auth_url}\n")
    print("Waiting for callback… (Ctrl+C to cancel)\n")
    webbrowser.open(auth_url)

    _done_event.clear()
    _auth_result.clear()

    try:
        while not _done_event.is_set():
            server.handle_request()
    except KeyboardInterrupt:
        print("\nCancelled.")
    finally:
        server.server_close()
        if tunnel_proc:
            tunnel_proc.kill()

    if "error" in _auth_result:
        raise RuntimeError(f"Auth failed: {_auth_result['error']}")

    if "tokens" in _auth_result:
        tokens = _auth_result["tokens"]
        tokens["obtained_at"] = int(time.time())
        save_tokens(tokens)
        print(f"\n✅ Authentication successful! Tokens saved to {TOKEN_FILE}")
        print("You can now run: python -m src.cli status\n")
        return tokens

    print("\nNo tokens received — the callback page may have shown a manual")
    print("code you can exchange with:  python -m src.daikin.auth --code CODE")
    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "--setup":
        run_setup()
    elif len(sys.argv) >= 3 and sys.argv[1] == "--code":
        run_auth_flow_with_code(sys.argv[2])
    else:
        run_auth_flow()
