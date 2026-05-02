"""SmartThings public REST API client (OAuth bearer token).

API: https://developer.smartthings.com/docs/api/public

Phase 1 surface — only what the appliance scheduler needs:
- list devices (discovery)
- read remoteControlStatus.remoteControlEnabled (trigger)
- read full status (cycle metadata, best-effort)
- POST setMachineState run (the fire path)

Bearer access token is sourced from :mod:`src.smartthings.auth` (OAuth flow
+ refresh-on-stale). On HTTP 401 the client refreshes once and retries —
covers token revocation / clock skew / Samsung's silent rotation.

Honours ``OPENCLAW_READ_ONLY``: when true, ``start_cycle`` returns
``{"skipped": "read_only"}`` without making an HTTP request.

Errors are surfaced as :class:`SmartThingsError` with a short ``code`` so
callers can match on ``"auth_invalid"`` (401 even after refresh),
``"not_found"`` (404), etc.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from ..config import config
from . import auth as _auth

logger = logging.getLogger(__name__)


class SmartThingsError(Exception):
    """Raised on any SmartThings REST error.

    ``code`` is a short machine-readable label (``auth_invalid``,
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

    No retry on most 4xx; one retry on 5xx; one refresh+retry on 401.
    SmartThings is not Daikin-quota-constrained — the appliance dispatcher
    reads remote_mode at most once per LP solve (~50 reads/day total).

    Pass ``access_token=None`` (default) to source from the OAuth file via
    :func:`src.smartthings.auth.get_valid_access_token`. Pass an explicit
    token only for tests.
    """

    def __init__(self, access_token: str | None = None, base_url: str | None = None) -> None:
        self._explicit_token = access_token.strip() if access_token else None
        self._base = (base_url or config.SMARTTHINGS_API_BASE).rstrip("/")

    def _bearer(self, *, force_refresh: bool = False) -> str:
        """Return a bearer token — explicit override or OAuth-managed."""
        if self._explicit_token is not None:
            return self._explicit_token
        return _auth.get_valid_access_token(force_refresh=force_refresh)

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

    def get_machine_state(self, device_id: str) -> tuple[str | None, str | None]:
        """Return ``(machineState_value, machineState_timestamp_iso)``.

        Samsung reports both the current value AND the ISO timestamp of the
        last state transition. PR #235 — using that timestamp for the
        ``ended_at`` field on the cycle-completion notification (instead of
        ``now()``) eliminates the up-to-5-min skew from the reconcile cadence.

        Both elements default to ``None`` when the capability isn't present
        or the value isn't parseable. Used by the completion poller
        (PR #234) — caller treats anything that isn't ``run`` or ``pause``
        as "cycle has ended".
        """
        try:
            data = self._request(
                "GET",
                f"/devices/{device_id}/components/main/capabilities/washerOperatingState/status",
            )
        except SmartThingsError:
            return None, None
        attr = data.get("machineState") if isinstance(data, dict) else None
        if not isinstance(attr, dict):
            return None, None
        v = attr.get("value")
        ts_raw = attr.get("timestamp")
        ts: str | None = ts_raw.strip() if isinstance(ts_raw, str) and ts_raw.strip() else None
        if isinstance(v, str):
            return v.strip().lower(), ts
        return None, ts

    def get_power_consumption_energy_wh(self, device_id: str) -> int | None:
        """Return ``powerConsumptionReport.powerConsumption.energy`` in Wh.

        This is a **cumulative lifetime counter** (monotonic; only resets on
        firmware reset). Subtract two snapshots taken at cycle fire-time and
        completion to get the actual energy consumed by the cycle (PR #235).
        Returns ``None`` when the capability isn't reported by the device or
        the value isn't an integer.
        """
        try:
            data = self._request(
                "GET",
                f"/devices/{device_id}/components/main/capabilities/powerConsumptionReport/status",
            )
        except SmartThingsError:
            return None
        pc = data.get("powerConsumption") if isinstance(data, dict) else None
        val = pc.get("value") if isinstance(pc, dict) else None
        if not isinstance(val, dict):
            return None
        e = val.get("energy")
        if isinstance(e, (int, float)):
            return int(e)
        return None

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
        if body is not None:
            payload = json.dumps(body).encode()

        # Up to 3 attempts: (1) initial, (2) 401-refresh, (3) 5xx-retry.
        # ``forced_refresh`` ensures we only refresh once per call so a stale
        # refresh_token can't push us into an infinite loop.
        forced_refresh = False
        last_5xx_retried = False
        for attempt in range(3):
            try:
                token = self._bearer(force_refresh=(forced_refresh and attempt == 1))
            except _auth.SmartThingsAuthError as e:
                raise SmartThingsError("auth_missing", str(e)) from e
            except _auth.SmartThingsAuthCircuitOpen as e:
                raise SmartThingsError("auth_circuit_open", str(e)) from e
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            if payload is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=payload, headers=headers, method=method)
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
                if e.code == 401 and not forced_refresh and self._explicit_token is None:
                    logger.info(
                        "smartthings %s %s → 401, refreshing access token + retry",
                        method, path,
                    )
                    forced_refresh = True
                    continue
                if e.code == 401:
                    raise SmartThingsError(
                        "auth_invalid", err_body or "access token rejected",
                        http_status=401,
                    ) from None
                if e.code == 404:
                    raise SmartThingsError(
                        "not_found", err_body or "device or capability not found",
                        http_status=404,
                    ) from None
                if 500 <= e.code < 600 and not last_5xx_retried:
                    logger.warning(
                        "smartthings %s %s → HTTP %d, retrying once",
                        method, path, e.code,
                    )
                    last_5xx_retried = True
                    continue
                raise SmartThingsError(
                    "http_error", f"HTTP {e.code}: {err_body}".rstrip(),
                    http_status=e.code,
                ) from None
            except urllib.error.URLError as e:
                if not last_5xx_retried:
                    logger.warning(
                        "smartthings %s %s → transport error %s, retrying once",
                        method, path, e,
                    )
                    last_5xx_retried = True
                    continue
                raise SmartThingsError("transport", str(e)) from None
            except OSError as e:
                if not last_5xx_retried:
                    last_5xx_retried = True
                    continue
                raise SmartThingsError("transport", str(e)) from None
        raise SmartThingsError("transport", "unreachable code path")
