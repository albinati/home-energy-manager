"""Fetch Octopus Agile half-hourly rates (public API, no auth).

Supports both import and export tariff codes. Export rates are used by the
solver to decide when to force-discharge the battery to grid during peak windows.
"""
import json
import logging
import re
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional

from ..config import config

logger = logging.getLogger(__name__)

OCTOPUS_BASE = "https://api.octopus.energy/v1"


def _tariff_to_product(tariff_code: str) -> str:
    """Derive product code from full tariff code (e.g. E-1R-AGILE-24-10-01-C -> AGILE-24-10-01)."""
    parts = tariff_code.split("-")
    try:
        idx = next(i for i, p in enumerate(parts) if p == "AGILE")
        return "-".join(parts[idx : -1])
    except StopIteration:
        return "AGILE-24-10-01"


def _fetch_rates(tariff_code: str, period_from: datetime, period_to: datetime) -> list[dict]:
    """Core HTTP fetch: get standard-unit-rates for any tariff code."""
    product = _tariff_to_product(tariff_code)
    url = (
        f"{OCTOPUS_BASE}/products/{product}/electricity-tariffs/{tariff_code}/standard-unit-rates/"
        f"?period_from={period_from.isoformat().replace('+00:00', 'Z')}"
        f"&period_to={period_to.isoformat().replace('+00:00', 'Z')}"
        "&page_size=96"
    )
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        logger.warning("Octopus rates fetch failed (%s): %s", tariff_code, exc)
        return []

    results = data.get("results") or []
    return [
        {
            "value_inc_vat": r.get("value_inc_vat"),
            "valid_from": r.get("valid_from"),
            "valid_to": r.get("valid_to"),
        }
        for r in results
        if r.get("value_inc_vat") is not None
    ]


def fetch_agile_rates(
    tariff_code: Optional[str] = None,
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
) -> list[dict]:
    """Fetch standard unit rates for the given Agile import tariff. No API key required.

    Returns list of dicts with keys: value_inc_vat (p/kWh), valid_from, valid_to (ISO strings).
    """
    code = (tariff_code or config.OCTOPUS_TARIFF_CODE).strip()
    if not code:
        return []

    now = datetime.now(timezone.utc)
    if period_from is None:
        period_from = now.replace(minute=0, second=0, microsecond=0)
    if period_to is None:
        period_to = period_from + timedelta(hours=48)

    return _fetch_rates(code, period_from, period_to)


def fetch_agile_export_rates(
    export_tariff_code: Optional[str] = None,
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
) -> list[dict]:
    """Fetch Agile export (outgoing) rates. No API key required.

    The export tariff code is typically the AGILE-EXPORT equivalent of the import tariff.
    Set OCTOPUS_EXPORT_TARIFF_CODE in .env (e.g. E-1R-AGILE-OUTGOING-24-10-01-C).

    Returns [] if no export tariff code is configured.
    """
    code = (export_tariff_code or config.OCTOPUS_EXPORT_TARIFF_CODE or "").strip()
    if not code:
        return []

    now = datetime.now(timezone.utc)
    if period_from is None:
        period_from = now.replace(minute=0, second=0, microsecond=0)
    if period_to is None:
        period_to = period_from + timedelta(hours=48)

    return _fetch_rates(code, period_from, period_to)


def get_current_and_next_slots(
    rates: list[dict],
    cheap_threshold_pence: float,
    peak_start: str,
    peak_end: str,
) -> tuple[Optional[dict], Optional[dict], Optional[float]]:
    """Return (current_slot, next_cheap_slot, current_price_pence).

    current_slot / next_cheap_slot are rate dicts with value_inc_vat, valid_from, valid_to.
    peak_start/peak_end are "HH:MM" strings (24h); slots in that window are treated as peak.
    """
    if not rates:
        return None, None, None

    now = datetime.now(timezone.utc)
    peak_s = _parse_time(peak_start)
    peak_e = _parse_time(peak_end)

    current: Optional[dict] = None
    current_price: Optional[float] = None
    next_cheap: Optional[dict] = None

    for r in rates:
        valid_from = _parse_iso(r.get("valid_from"))
        valid_to = _parse_iso(r.get("valid_to"))
        if valid_from is None or valid_to is None:
            continue
        price = float(r.get("value_inc_vat", 0))
        if valid_from <= now < valid_to:
            current = r
            current_price = price
        is_cheap = price <= cheap_threshold_pence
        slot_start = valid_from.time()
        is_peak = peak_s <= slot_start <= peak_e if peak_s and peak_e else False
        if is_cheap and not is_peak and valid_from > now and next_cheap is None:
            next_cheap = r

    return current, next_cheap, current_price


def _parse_time(s: str) -> Optional[object]:
    """Parse HH:MM to a time (for comparison)."""
    m = re.match(r"(\d{1,2}):(\d{2})", str(s).strip())
    if not m:
        return None
    from datetime import time

    return time(int(m.group(1)), int(m.group(2)))


def _parse_iso(s: Optional[str]):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
