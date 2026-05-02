"""SmartThings service — lazy singleton, PAT loaded from
:data:`config.SMARTTHINGS_TOKEN_FILE`.

Mirrors :mod:`src.daikin.service` shape. The PAT lives at a 0600 file
on disk (matching the convention for ``.daikin-tokens.json`` and
``.openclaw-token``); the credentials endpoint writes it.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from ..config import config
from .client import SmartThingsClient, SmartThingsError

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_client: SmartThingsClient | None = None


def _resolve_token_path() -> Path:
    p = Path(config.SMARTTHINGS_TOKEN_FILE)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


def _load_pat() -> str:
    """Read the PAT from disk. Raises :class:`SmartThingsError` if absent."""
    p = _resolve_token_path()
    if not p.is_file():
        raise SmartThingsError(
            "pat_missing",
            f"token file {p} not present — POST /api/v1/integrations/smartthings/credentials first",
        )
    pat = p.read_text(encoding="utf-8").strip()
    if not pat:
        raise SmartThingsError("pat_missing", f"token file {p} is empty")
    return pat


def get_client() -> SmartThingsClient:
    """Return the lazy-init singleton SmartThingsClient.

    Raises :class:`SmartThingsError` (``code='pat_missing'``) when the PAT
    file is absent — callers can handle this as "integration not configured"
    without crashing the LP solve.
    """
    global _client
    with _lock:
        if _client is None:
            pat = _load_pat()
            _client = SmartThingsClient(pat=pat, base_url=config.SMARTTHINGS_API_BASE)
        return _client


def reset_client() -> None:
    """Drop the cached client so the next ``get_client()`` re-reads the PAT.

    Called after the credentials endpoint writes a new token, or after a 401
    failure surfaces.
    """
    global _client
    with _lock:
        _client = None


def write_pat(pat: str) -> Path:
    """Persist a PAT to ``config.SMARTTHINGS_TOKEN_FILE`` with 0600 perms.

    Returns the resolved path. Caller is responsible for round-trip validation
    (e.g. ``list_devices``) BEFORE accepting the token from a user.
    """
    if not pat or not pat.strip():
        raise SmartThingsError("pat_missing", "refusing to write empty PAT")
    p = _resolve_token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(pat.strip() + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        logger.debug("Could not chmod %s to 0600 (continuing)", p)
    reset_client()
    return p


def delete_pat() -> bool:
    """Remove the PAT file. Returns True if a file was removed."""
    p = _resolve_token_path()
    if not p.exists():
        return False
    p.unlink()
    reset_client()
    return True


def pat_present() -> bool:
    """Cheap check used by the status endpoint — does NOT load the file."""
    return _resolve_token_path().is_file()
