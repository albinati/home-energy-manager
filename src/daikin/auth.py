"""Daikin Onecta OAuth2 auth flow.

Run once to authenticate:
    python -m src.daikin.auth

This uses a manual copy-paste flow — no local server required:
  1. A browser opens with the Daikin login page
  2. You log in with your Daikin/Onecta account
  3. The browser tries to redirect to localhost and shows a "connection refused" error
  4. Copy the full URL from the browser address bar and paste it when prompted
  5. Tokens are saved to .daikin-tokens.json

In the Daikin developer portal, set redirect URI to:
    http://localhost:8080/callback

Developer portal: https://developer.cloud.daikineurope.com
"""
import json
import time
import urllib.request
import urllib.parse
import webbrowser
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from ..config import config


TOKEN_FILE = config.DAIKIN_TOKEN_FILE


def run_auth_flow() -> dict:
    """Run the OAuth2 authorisation code flow — manual copy-paste, no local server."""
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

    print("\n" + "=" * 60)
    print("DAIKIN ONECTA — AUTHENTICATION")
    print("=" * 60)
    print("\nStep 1: Opening browser. Log in with your Daikin/Onecta account.")
    print(f"\nIf the browser doesn't open, visit this URL manually:\n{auth_url}\n")
    webbrowser.open(auth_url)

    print("Step 2: After logging in, the browser will show a")
    print("  'connection refused' or 'site can't be reached' error.")
    print("  That's expected. Copy the FULL URL from the address bar.\n")

    callback_url = input("Step 3: Paste the full URL here and press Enter:\n> ").strip()

    # Extract code from URL
    parsed = urlparse(callback_url)
    params_from_url = parse_qs(parsed.query)

    if "error" in params_from_url:
        raise RuntimeError(f"Auth error: {params_from_url.get('error_description', params_from_url['error'])}")

    code = params_from_url.get("code", [None])[0]
    if not code:
        # Maybe they pasted just the code
        if len(callback_url) < 200 and "=" not in callback_url:
            code = callback_url
        else:
            raise RuntimeError(
                "Could not find 'code' in the URL.\n"
                "Make sure you copied the full URL from the address bar."
            )

    received_state = params_from_url.get("state", [None])[0]
    if received_state and received_state != state:
        print("Warning: state mismatch — proceeding anyway.")

    print("\nExchanging code for tokens...")

    # Exchange code for tokens
    post_data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.DAIKIN_REDIRECT_URI,
        "client_id": config.DAIKIN_CLIENT_ID,
        "client_secret": config.DAIKIN_CLIENT_SECRET,
    }).encode()

    req = urllib.request.Request(
        config.DAIKIN_TOKEN_URL,
        data=post_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        resp = urllib.request.urlopen(req)
        tokens = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"Token exchange failed ({e.code}): {body[:300]}")

    tokens["obtained_at"] = int(time.time())
    save_tokens(tokens)

    print(f"\n✅ Authentication successful! Tokens saved to {TOKEN_FILE}")
    print("You can now run: python -m src.cli status\n")
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
    post_data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": config.DAIKIN_CLIENT_ID,
        "client_secret": config.DAIKIN_CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(
        config.DAIKIN_TOKEN_URL,
        data=post_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = urllib.request.urlopen(req)
    new_tokens = json.loads(resp.read())
    new_tokens["obtained_at"] = int(time.time())
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
