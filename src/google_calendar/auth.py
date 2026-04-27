"""Google Calendar credentials — service account (preferred) or OAuth.

Resolution order in ``load_credentials()``:

1. **Service account** — if ``GOOGLE_CALENDAR_SA_FILE`` exists, use it.
   No refresh tokens, no browser dance. The family calendar must be shared
   with the SA's email ("Make changes to events" permission).
2. **OAuth installed app** — fallback. Reads cached tokens at
   ``GOOGLE_CALENDAR_TOKEN_FILE`` (bootstrapped by ``run_oauth_flow``).

The publisher catches ``GoogleCalendarAuthError`` and logs a one-line
warning rather than failing the scheduler tick.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import config

logger = logging.getLogger(__name__)

# Narrow scope: create/update/delete events on calendars the user has write
# access to. Does NOT grant read access to private events on other calendars.
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


class GoogleCalendarAuthError(RuntimeError):
    """No usable Google Calendar credentials available."""


def _sa_path() -> Path:
    return Path(config.GOOGLE_CALENDAR_SA_FILE).expanduser()


def _token_path() -> Path:
    return Path(config.GOOGLE_CALENDAR_TOKEN_FILE).expanduser()


def _client_secret_path() -> Path:
    return Path(config.GOOGLE_CALENDAR_CLIENT_SECRET_FILE).expanduser()


def load_credentials():
    """Return Credentials for the Google Calendar API, or raise.

    Service account first; falls back to OAuth installed-app tokens.
    Lazy-imports the google-auth packages so the rest of the codebase keeps
    working even when this side feature's deps aren't installed.
    """
    sa_path = _sa_path()
    if sa_path.exists():
        try:
            from google.oauth2 import service_account
        except ImportError as e:
            raise GoogleCalendarAuthError(
                "google-auth not installed; pip install google-auth google-api-python-client"
            ) from e
        try:
            return service_account.Credentials.from_service_account_file(
                str(sa_path), scopes=SCOPES,
            )
        except Exception as e:
            raise GoogleCalendarAuthError(f"service account load failed: {e}") from e

    # OAuth fallback.
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:
        raise GoogleCalendarAuthError(
            "google-auth not installed; pip install google-auth google-api-python-client"
        ) from e

    token_path = _token_path()
    if not token_path.exists():
        raise GoogleCalendarAuthError(
            f"No Google credentials. Either drop a service-account key at {sa_path} "
            f"or run the OAuth bootstrap: docker compose -f compose.google-auth.yaml run --rm google-auth"
        )

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            raise GoogleCalendarAuthError(f"refresh failed: {e}") from e
        token_path.write_text(creds.to_json())
        logger.debug("Google Calendar access token refreshed")

    if not creds.valid:
        raise GoogleCalendarAuthError("credentials invalid after refresh attempt")

    return creds


def run_oauth_flow() -> None:
    """Interactive installed-app OAuth flow. Persists tokens then exits."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        raise GoogleCalendarAuthError(
            "google-auth-oauthlib not installed; pip install google-auth-oauthlib"
        ) from e

    secret_path = _client_secret_path()
    if not secret_path.exists():
        raise GoogleCalendarAuthError(
            f"OAuth client secret not found at {secret_path}.\n"
            "1. Create an OAuth 2.0 Client ID (Desktop app type) at "
            "https://console.cloud.google.com/apis/credentials\n"
            "2. Download the JSON and place it at the path above.\n"
            "3. Re-run this bootstrap."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
    port = config.GOOGLE_CALENDAR_OAUTH_PORT
    logger.info(
        "Starting OAuth flow on port %d. Open the URL below in a browser "
        "(SSH-tunnel :%d to your workstation if running on a remote host).",
        port, port,
    )
    creds = flow.run_local_server(port=port, open_browser=False)

    token_path = _token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    logger.info("Google Calendar tokens written to %s", token_path)
