"""SmartThings service — singleton client backed by the OAuth token file.

Mirrors :mod:`src.daikin.service` shape. The token file at
``config.SMARTTHINGS_TOKEN_FILE`` is written by the one-shot enrollment
container (``deploy/compose.smartthings-auth.yaml``) and refreshed
automatically by :mod:`src.smartthings.auth`.
"""
from __future__ import annotations

import logging
import threading

from ..config import config
from . import auth as _auth
from .client import SmartThingsClient

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_client: SmartThingsClient | None = None


def get_client() -> SmartThingsClient:
    """Return the lazy-init singleton SmartThingsClient.

    The client sources its bearer token from
    :func:`src.smartthings.auth.get_valid_access_token` on every request
    (auto-refresh on stale, 401-retry on revoked). No PAT.

    Raises :class:`src.smartthings.auth.SmartThingsAuthError` from the first
    request if the token file is absent — callers can catch this as
    "integration not configured" without crashing the LP solve.
    """
    global _client
    with _lock:
        if _client is None:
            _client = SmartThingsClient(base_url=config.SMARTTHINGS_API_BASE)
        return _client


def reset_client() -> None:
    """Drop the cached client. Mostly for tests + the rare revocation path."""
    global _client
    with _lock:
        _client = None


def tokens_present() -> bool:
    """Cheap check used by the status endpoint — does NOT load the file body."""
    return _auth.has_tokens()


def delete_tokens() -> bool:
    """Remove the token file (revokes local OAuth state). Returns True if removed."""
    from pathlib import Path

    p = Path(config.SMARTTHINGS_TOKEN_FILE)
    if not p.is_absolute():
        p = __import__("pathlib").Path.cwd() / p
    if not p.exists():
        return False
    p.unlink()
    reset_client()
    return True
