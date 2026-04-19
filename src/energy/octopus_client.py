"""Authenticated Octopus Energy REST client.

Uses HTTP Basic Auth with the API key as username and empty password.
All endpoints require authentication except the public product catalogue
(handled by octopus_products.py).

Key capabilities:
- fetch_account(): tariff agreements, MPAN roles (import/export), GSP
- fetch_consumption(): half-hourly or aggregated smart meter data
- fetch_half_hourly_consumption(): aligned import+export slot data
- auto_detect_mpan_roles(): determine which MPAN is import vs export
- discover_current_tariff(): active tariff from account agreements
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import config

logger = logging.getLogger(__name__)

OCTOPUS_BASE = "https://api.octopus.energy/v1"

_ROLE_CACHE: dict[str, Any] = {}  # runtime cache, reset on auto-detect


@dataclass
class ConsumptionSlot:
    interval_start: datetime
    interval_end: datetime
    consumption_kwh: float


@dataclass
class HalfHourlyData:
    import_slots: list[ConsumptionSlot] = field(default_factory=list)
    export_slots: list[ConsumptionSlot] = field(default_factory=list)


@dataclass
class CurrentTariff:
    product_code: str
    tariff_code: str
    gsp: str
    valid_from: datetime | None
    valid_to: datetime | None


@dataclass
class MpanRoles:
    import_mpan: str
    import_serial: str
    export_mpan: str
    export_serial: str
    gsp: str
    source: str  # "account_api" or "config_fallback"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _auth_header() -> str:
    """Return HTTP Basic Auth header value for the API key."""
    key = config.OCTOPUS_API_KEY
    if not key:
        raise ValueError(
            "OCTOPUS_API_KEY not configured. "
            "Add it to .env to use authenticated Octopus endpoints."
        )
    token = base64.b64encode(f"{key}:".encode()).decode()
    return f"Basic {token}"


def _get_json(url: str, timeout: int = 15) -> Any:
    """Authenticated GET, returns parsed JSON."""
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Authorization": _auth_header()},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace") if exc.fp else ""
        raise RuntimeError(
            f"Octopus API {exc.code} for {url}: {body[:200]}"
        ) from exc


def _paginate(
    url: str,
    *,
    page_size: int = 25000,
    max_results: int = 100000,
) -> list[dict]:
    """Fetch all pages from a paginated Octopus endpoint."""
    results: list[dict] = []
    next_url: str | None = f"{url}{'&' if '?' in url else '?'}page_size={page_size}"
    while next_url and len(results) < max_results:
        data = _get_json(next_url)
        batch = data.get("results") or []
        results.extend(batch)
        next_url = data.get("next")
    return results


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _extract_gsp_from_tariff_code(tariff_code: str) -> str | None:
    """E-1R-AGILE-24-10-01-H -> 'H'"""
    parts = tariff_code.upper().split("-")
    if parts and len(parts[-1]) == 1 and parts[-1].isalpha():
        return parts[-1]
    return None


def _extract_product_from_tariff_code(tariff_code: str) -> str | None:
    """E-1R-AGILE-24-10-01-H -> 'AGILE-24-10-01'"""
    # Format: E-{1R|2R}-<PRODUCT-CODE>-<GSP>
    # Strip leading E-1R- or E-2R- and trailing -<GSP>
    import re
    m = re.match(r"^E-[12]R-(.+)-[A-P]$", tariff_code.upper())
    if m:
        return m.group(1)
    return None


# ── Public interface ──────────────────────────────────────────────────────────

def fetch_account(account_number: str | None = None) -> dict:
    """Fetch account details from Octopus API.

    Returns the full account response including:
    - properties[].electricity_meter_points[].mpan
    - properties[].electricity_meter_points[].is_export
    - properties[].electricity_meter_points[].agreements[].tariff_code
    - properties[].electricity_meter_points[].meters[].serial_number
    """
    number = account_number or config.OCTOPUS_ACCOUNT_NUMBER
    if not number:
        raise ValueError(
            "OCTOPUS_ACCOUNT_NUMBER not configured. "
            "Add OCTOPUS_ACCOUNT_NUMBER=A-XXXXXXXX to .env."
        )
    url = f"{OCTOPUS_BASE}/accounts/{number}/"
    return _get_json(url)


def fetch_consumption(
    mpan: str,
    serial: str,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
    *,
    group_by: str | None = None,
    order: str = "asc",
) -> list[ConsumptionSlot]:
    """Fetch electricity consumption for a meter point.

    Args:
        mpan: Meter Point Administration Number
        serial: Meter serial number
        period_from: Start of period (UTC). Defaults to 30 days ago.
        period_to: End of period (UTC). Defaults to now.
        group_by: Aggregation — None (half-hourly), "day", "week", "month"
        order: "asc" (oldest first) or "desc"

    Returns:
        List of ConsumptionSlot ordered by interval_start.
    """
    if period_from is None:
        period_from = datetime.now(UTC) - timedelta(days=30)
    if period_to is None:
        period_to = datetime.now(UTC)

    url = (
        f"{OCTOPUS_BASE}/electricity-meter-points/{mpan}"
        f"/meters/{serial}/consumption/"
    )
    params: dict[str, str] = {
        "period_from": period_from.isoformat(),
        "period_to": period_to.isoformat(),
        "order_by": order,
    }
    if group_by:
        params["group_by"] = group_by

    full_url = url + "?" + urllib.parse.urlencode(params)
    raw = _paginate(full_url)

    slots: list[ConsumptionSlot] = []
    for r in raw:
        start = _parse_dt(r.get("interval_start"))
        end = _parse_dt(r.get("interval_end"))
        kwh = r.get("consumption")
        if start and end and kwh is not None:
            slots.append(ConsumptionSlot(
                interval_start=start,
                interval_end=end,
                consumption_kwh=float(kwh),
            ))
    return slots


def fetch_half_hourly_consumption(
    period_from: datetime | None = None,
    period_to: datetime | None = None,
) -> HalfHourlyData:
    """Fetch aligned half-hourly import and export consumption.

    Uses the resolved import/export MPAN and serial from config.
    Falls back to auto_detect_mpan_roles() if MPANs not explicitly assigned.
    """
    roles = get_mpan_roles()
    result = HalfHourlyData()

    if roles.import_mpan and roles.import_serial:
        try:
            result.import_slots = fetch_consumption(
                roles.import_mpan,
                roles.import_serial,
                period_from,
                period_to,
            )
            logger.info(
                "Fetched %d import half-hourly slots from Octopus",
                len(result.import_slots),
            )
        except Exception as exc:
            logger.warning("Import consumption fetch failed: %s", exc)

    if roles.export_mpan and roles.export_serial:
        try:
            result.export_slots = fetch_consumption(
                roles.export_mpan,
                roles.export_serial,
                period_from,
                period_to,
            )
            logger.info(
                "Fetched %d export half-hourly slots from Octopus",
                len(result.export_slots),
            )
        except Exception as exc:
            logger.warning("Export consumption fetch failed: %s", exc)

    return result


def get_mpan_roles(*, force_refresh: bool = False) -> MpanRoles:
    """Return import/export MPAN roles.

    Priority:
    1. Runtime cache (populated by auto_detect_mpan_roles or previous call)
    2. Explicit env vars OCTOPUS_MPAN_IMPORT / OCTOPUS_MPAN_EXPORT
    3. Account API auto-detection (if account number + API key present)
    4. Config fallback: MPAN_1 = import, MPAN_2 = export
    """
    if not force_refresh and _ROLE_CACHE:
        return MpanRoles(**_ROLE_CACHE)

    # Try explicit env vars first
    if config.OCTOPUS_MPAN_IMPORT and config.OCTOPUS_MPAN_EXPORT:
        roles = MpanRoles(
            import_mpan=config.OCTOPUS_MPAN_IMPORT,
            import_serial=config.OCTOPUS_METER_SERIAL_IMPORT,
            export_mpan=config.OCTOPUS_MPAN_EXPORT,
            export_serial=config.OCTOPUS_METER_SERIAL_EXPORT,
            gsp=config.OCTOPUS_GSP,
            source="config_fallback",
        )
        # Still try account API for GSP and role confirmation
        if config.OCTOPUS_ACCOUNT_NUMBER and config.OCTOPUS_API_KEY:
            try:
                detected = auto_detect_mpan_roles()
                return detected
            except Exception as exc:
                logger.debug("Account API detection skipped, using config: %s", exc)
        _ROLE_CACHE.update(vars(roles))
        return roles

    # No explicit split — try account API
    if config.OCTOPUS_ACCOUNT_NUMBER and config.OCTOPUS_API_KEY:
        try:
            return auto_detect_mpan_roles()
        except Exception as exc:
            logger.warning("MPAN auto-detect failed: %s", exc)

    # Final fallback: MPAN_1=import, MPAN_2=export
    roles = MpanRoles(
        import_mpan=config.OCTOPUS_MPAN_1,
        import_serial=config.OCTOPUS_METER_SN_1,
        export_mpan=config.OCTOPUS_MPAN_2,
        export_serial=config.OCTOPUS_METER_SN_2,
        gsp=config.OCTOPUS_GSP,
        source="config_fallback",
    )
    _ROLE_CACHE.update(vars(roles))
    return roles


def auto_detect_mpan_roles() -> MpanRoles:
    """Detect which MPAN is import vs export from the account endpoint.

    Uses the `is_export` flag on each meter point.
    Updates the runtime cache.
    """
    account_data = fetch_account()
    properties = account_data.get("properties") or []

    import_mpan = import_serial = ""
    export_mpan = export_serial = ""
    gsp = config.OCTOPUS_GSP

    for prop in properties:
        for mp in (prop.get("electricity_meter_points") or []):
            mpan = mp.get("mpan") or ""
            is_exp = bool(mp.get("is_export"))
            meters = mp.get("meters") or []
            serial = meters[0].get("serial_number") or "" if meters else ""

            # Extract GSP from agreements if available
            for agreement in (mp.get("agreements") or []):
                tc = agreement.get("tariff_code") or ""
                detected_gsp = _extract_gsp_from_tariff_code(tc)
                if detected_gsp:
                    gsp = detected_gsp

            if is_exp:
                export_mpan = mpan
                export_serial = serial
            else:
                import_mpan = mpan
                import_serial = serial

    # If we got both, great. If not, fall back to config order
    if not import_mpan:
        import_mpan = config.OCTOPUS_MPAN_1
        import_serial = config.OCTOPUS_METER_SN_1
    if not export_mpan:
        export_mpan = config.OCTOPUS_MPAN_2
        export_serial = config.OCTOPUS_METER_SN_2

    roles = MpanRoles(
        import_mpan=import_mpan,
        import_serial=import_serial,
        export_mpan=export_mpan,
        export_serial=export_serial,
        gsp=gsp,
        source="account_api",
    )
    _ROLE_CACHE.update(vars(roles))
    logger.info(
        "MPAN roles detected: import=%s export=%s GSP=%s",
        import_mpan,
        export_mpan,
        gsp,
    )
    return roles


def discover_current_tariff() -> CurrentTariff | None:
    """Find the currently active electricity import tariff from account agreements.

    Returns None if account data unavailable or no active agreement found.
    """
    try:
        account_data = fetch_account()
    except Exception as exc:
        logger.warning("Cannot discover tariff: %s", exc)
        return None

    now = datetime.now(UTC)
    properties = account_data.get("properties") or []

    for prop in properties:
        for mp in (prop.get("electricity_meter_points") or []):
            # Skip export meter points
            if mp.get("is_export"):
                continue
            for agreement in (mp.get("agreements") or []):
                tariff_code = agreement.get("tariff_code") or ""
                if not tariff_code:
                    continue
                valid_from = _parse_dt(agreement.get("valid_from"))
                valid_to = _parse_dt(agreement.get("valid_to"))
                # Check if this agreement is currently active
                from_ok = valid_from is None or valid_from <= now
                to_ok = valid_to is None or valid_to > now
                if from_ok and to_ok:
                    product_code = _extract_product_from_tariff_code(tariff_code) or ""
                    gsp = _extract_gsp_from_tariff_code(tariff_code) or config.OCTOPUS_GSP
                    logger.info(
                        "Current tariff discovered: product=%s tariff=%s gsp=%s",
                        product_code,
                        tariff_code,
                        gsp,
                    )
                    return CurrentTariff(
                        product_code=product_code,
                        tariff_code=tariff_code,
                        gsp=gsp,
                        valid_from=valid_from,
                        valid_to=valid_to,
                    )

    logger.warning("No active electricity agreement found in account data")
    return None


def get_account_summary() -> dict:
    """Return a structured summary of account details for API/dashboard use.

    Includes: account number, current tariff, MPAN roles, GSP, data status.
    """
    summary: dict[str, Any] = {
        "account_number": config.OCTOPUS_ACCOUNT_NUMBER,
        "api_key_configured": bool(config.OCTOPUS_API_KEY),
        "current_tariff": None,
        "mpan_import": None,
        "mpan_export": None,
        "gsp": config.OCTOPUS_GSP,
        "detection_source": "not_run",
        "error": None,
    }

    if not config.OCTOPUS_API_KEY:
        summary["error"] = "OCTOPUS_API_KEY not configured"
        return summary

    try:
        tariff = discover_current_tariff()
        if tariff:
            summary["current_tariff"] = {
                "product_code": tariff.product_code,
                "tariff_code": tariff.tariff_code,
                "gsp": tariff.gsp,
                "valid_from": tariff.valid_from.isoformat() if tariff.valid_from else None,
                "valid_to": tariff.valid_to.isoformat() if tariff.valid_to else None,
            }
            summary["gsp"] = tariff.gsp
    except Exception as exc:
        summary["error"] = str(exc)

    try:
        roles = auto_detect_mpan_roles()
        summary["mpan_import"] = roles.import_mpan
        summary["mpan_export"] = roles.export_mpan
        summary["detection_source"] = roles.source
        if not summary["error"]:
            summary["gsp"] = roles.gsp
    except Exception as exc:
        if not summary["error"]:
            summary["error"] = str(exc)

    return summary
