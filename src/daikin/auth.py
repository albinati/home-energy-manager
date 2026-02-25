"""Daikin Onecta OAuth2 auth flow.

Run once to get tokens:
    python -m src.daikin.auth

Tokens are saved to .daikin-tokens.json and refreshed automatically.

Developer portal: https://developer.cloud.daikineurope.com
"""
import json
import time
import urllib.request
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from ..config import config


TOKEN_FILE = config.DAIKIN_TOKEN_FILE


class _CallbackHandler(BaseHTTPRequestHandler):
    auth_code = None

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h1>Authorised. You can close this window.</h1>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h1>Error: no code in callback.</h1>")

    def log_message(self, *args):
        pass  # suppress request logs


def run_auth_flow() -> dict:
    """Run the OAuth2 authorisation code flow interactively."""
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

    print(f"\nOpening browser for Daikin login...\n{auth_url}\n")
    webbrowser.open(auth_url)

    # Start local callback server
    server = HTTPServer(("localhost", 8080), _CallbackHandler)
    print("Waiting for callback on http://localhost:8080/callback ...")
    server.handle_request()

    code = _CallbackHandler.auth_code
    if not code:
        raise RuntimeError("No auth code received.")

    # Exchange code for tokens
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.DAIKIN_REDIRECT_URI,
        "client_id": config.DAIKIN_CLIENT_ID,
        "client_secret": config.DAIKIN_CLIENT_SECRET,
    }).encode()

    req = urllib.request.Request(config.DAIKIN_TOKEN_URL, data=data)
    resp = urllib.request.urlopen(req)
    tokens = json.loads(resp.read())
    tokens["obtained_at"] = int(time.time())

    save_tokens(tokens)
    print(f"\nTokens saved to {TOKEN_FILE}")
    return tokens


def save_tokens(tokens: dict) -> None:
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))


def load_tokens() -> dict:
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(
            f"Token file not found: {TOKEN_FILE}\n"
            "Run: python -m src.daikin.auth"
        )
    return json.loads(TOKEN_FILE.read_text())


def refresh_tokens(tokens: dict) -> dict:
    """Refresh access token using refresh token."""
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": config.DAIKIN_CLIENT_ID,
        "client_secret": config.DAIKIN_CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(config.DAIKIN_TOKEN_URL, data=data)
    resp = urllib.request.urlopen(req)
    new_tokens = json.loads(resp.read())
    new_tokens["obtained_at"] = int(time.time())
    # Preserve refresh token if not rotated
    if "refresh_token" not in new_tokens:
        new_tokens["refresh_token"] = tokens["refresh_token"]
    save_tokens(new_tokens)
    return new_tokens


def get_valid_access_token() -> str:
    """Return a valid access token, refreshing if needed."""
    tokens = load_tokens()
    expires_in = tokens.get("expires_in", 3600)
    obtained_at = tokens.get("obtained_at", 0)
    if time.time() > obtained_at + expires_in - 60:
        tokens = refresh_tokens(tokens)
    return tokens["access_token"]


if __name__ == "__main__":
    run_auth_flow()
    print("Auth complete. You can now run the CLI.")
