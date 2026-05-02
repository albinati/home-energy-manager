"""SmartThings public REST API client (Personal Access Token).

API: https://developer.smartthings.com/docs/api/public

Phase 1 surface — only what the appliance scheduler needs:
- list devices (discovery)
- read remoteControlStatus.remoteControlEnabled (trigger)
- read full status (cycle metadata, best-effort)
- POST setMachineState run (the fire path)

Honours ``OPENCLAW_READ_ONLY``: when true, ``start_cycle`` returns
``{"skipped": "read_only"}`` without making an HTTP request.

Errors are surfaced as :class:`SmartThingsError` with a short ``code`` so
callers can match on ``"pat_invalid"`` (401), ``"not_found"`` (404), etc.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from ..config import config

logger = logging.getLogger(__name__)


class SmartThingsError(Exception):
    """Raised on any SmartThings REST error.

    ``code`` is a short machine-readable label (``pat_invalid``,
    ``not_found``, ``http_error``, ``transport``); ``http_status`` is the
    HTTP status code when the error came from a server response.
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        http_status: int | None = None,
    ) -> None:
        self.code = code
        self.http_status = http_status
        super().__init__(f"[{code}] {message}".rstrip())


class SmartThingsClient:
    """Thin sync wrapper around api.smartthings.com.

    No retry on 4xx; one retry on 5xx. SmartThings is not Daikin-quota-
    constrained — the appliance dispatcher reads remote_mode at most once
    per LP solve (~50 reads/day total).
    """

    def __init__(self, pat: str, base_url: str | None = None) -> None:
        if not pat:
            raise SmartThingsError("pat_missing", "PAT is empty")
        self._pat = pat.strip()
        self._base = (base_url or config.SMARTTHINGS_API_BASE).rstrip("/")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_devices(self) -> list[dict]:
        """GET /devices — returns the list of devices visible to this PAT."""
        data = self._request("GET", "/devices")
        items = data.get("items") if isinstance(data, dict) else None
        return list(items) if isinstance(items, list) else []

    def get_full_status(self, device_id: str) -> dict:
        """GET /devices/{id}/status — full capability map (best-effort cycle metadata)."""
        return self._request("GET", f"/devices/{device_id}/status")

    def get_remote_control_enabled(self, device_id: str) -> bool:
        """GET remoteControlStatus capability and parse remoteControlEnabled.

        Tolerates both string ("true"/"false") and native boolean response
        shapes — Samsung firmware varies. Returns False on any unparseable
        value rather than raising, since "no remote mode" is the correct
        default behaviour for the dispatcher.
        """
        data = self._request(
            "GET",
            f"/devices/{device_id}/components/main/capabilities/remoteControlStatus/status",
        )
        attr = (
            data.get("remoteControlEnabled") if isinstance(data, dict) else None
        )
        if not isinstance(attr, dict):
            return False
        v = attr.get("value")
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() == "true"
        return False

    def start_cycle(self, device_id: str) -> dict:
        """POST /devices/{id}/commands — washerOperatingState.setMachineState run.

        Honours ``OPENCLAW_READ_ONLY``: returns ``{"skipped": "read_only"}``
        without performing any HTTP call when the kill switch is engaged.
        """
        if config.OPENCLAW_READ_ONLY:
            logger.info(
                "smartthings.start_cycle skipped (OPENCLAW_READ_ONLY=true) device=%s",
                device_id,
            )
            return {"skipped": "read_only"}
        body = {
            "commands": [
                {
                    "component": "main",
                    "capability": "washerOperatingState",
                    "command": "setMachineState",
                    "arguments": ["run"],
                }
            ]
        }
        return self._request("POST", f"/devices/{device_id}/commands", body=body)

    def stop_cycle(self, device_id: str) -> dict:
        """POST /devices/{id}/commands — washerOperatingState.setMachineState stop.

        Phase 1 doesn't need this on the happy path (we abort by removing the
        APScheduler cron), but exposing it keeps the surface coherent for any
        manual-cancel path that wants to issue an explicit stop.
        """
        if config.OPENCLAW_READ_ONLY:
            return {"skipped": "read_only"}
        body = {
            "commands": [
                {
                    "component": "main",
                    "capability": "washerOperatingState",
                    "command": "setMachineState",
                    "arguments": ["stop"],
                }
            ]
        }
        return self._request("POST", f"/devices/{device_id}/commands", body=body)

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self._base}{path}"
        payload = None
        headers = {
            "Authorization": f"Bearer {self._pat}",
            "Accept": "application/json",
        }
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=payload, headers=headers, method=method)

        # No retry on 4xx; one retry on 5xx (transient).
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read()
                    if not raw:
                        return {}
                    try:
                        return json.loads(raw)
                    except ValueError as e:
                        raise SmartThingsError(
                            "bad_json", f"non-JSON response: {e}",
                            http_status=resp.getcode(),
                        ) from e
            except urllib.error.HTTPError as e:
                err_body = ""
                try:
                    err_body = e.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    pass
                if e.code == 401:
                    raise SmartThingsError(
                        "pat_invalid", err_body or "PAT rejected",
                        http_status=401,
                    ) from None
                if e.code == 404:
                    raise SmartThingsError(
                        "not_found", err_body or "device or capability not found",
                        http_status=404,
                    ) from None
                if 500 <= e.code < 600 and attempt == 0:
                    logger.warning(
                        "smartthings %s %s → HTTP %d, retrying once",
                        method, path, e.code,
                    )
                    continue
                raise SmartThingsError(
                    "http_error", f"HTTP {e.code}: {err_body}".rstrip(),
                    http_status=e.code,
                ) from None
            except urllib.error.URLError as e:
                if attempt == 0:
                    logger.warning(
                        "smartthings %s %s → transport error %s, retrying once",
                        method, path, e,
                    )
                    continue
                raise SmartThingsError("transport", str(e)) from None
            except OSError as e:
                if attempt == 0:
                    continue
                raise SmartThingsError("transport", str(e)) from None
        raise SmartThingsError("transport", "unreachable code path")
