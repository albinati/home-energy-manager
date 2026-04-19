"""Octopus Energy product catalogue — public API, no auth required.

Fetches the product list, per-product tariff details (standing charges,
unit rates, contract terms), and Agile/Tracker half-hourly rates.

All rates are inclusive of VAT (value_inc_vat) and in pence.

GSP (Grid Supply Point) determines the regional tariff code suffix (A-P).
Default is "C" (South East England / London).  Set OCTOPUS_GSP in .env
to override if your meter is in a different region.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from ..config import config
from .tariff_models import (
    ContractType,
    PricingStructure,
    RateSchedule,
    TariffPolicy,
    TariffProduct,
)

logger = logging.getLogger(__name__)

OCTOPUS_BASE = "https://api.octopus.energy/v1"

# Tariff code prefixes: E-1R = single register, E-2R = dual register (Economy 7)
_SINGLE_REG = "E-1R"
_DUAL_REG = "E-2R"

# Well-known product families by code prefix
_PRODUCT_PRICING: dict[str, PricingStructure] = {
    "AGILE": PricingStructure.HALF_HOURLY,
    "GO": PricingStructure.TIME_OF_USE,
    "SILVER": PricingStructure.TRACKER,
    "FLUX": PricingStructure.TIME_OF_USE,
    "INTELLI": PricingStructure.TIME_OF_USE,
    "COSY": PricingStructure.TIME_OF_USE,
}


def _gsp_suffix() -> str:
    """GSP letter for tariff codes. Default 'C' (London/SE)."""
    return (config.OCTOPUS_GSP if hasattr(config, "OCTOPUS_GSP") else "C").strip().upper() or "C"


def _get_json(url: str, timeout: int = 10) -> Any:
    """GET a URL and return parsed JSON."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _classify_pricing(product_code: str, is_variable: bool, is_tracker: bool) -> PricingStructure:
    """Determine pricing structure from product code and flags."""
    code_upper = product_code.upper()
    for prefix, ps in _PRODUCT_PRICING.items():
        if prefix in code_upper:
            return ps
    if is_tracker:
        return PricingStructure.TRACKER
    if is_variable:
        return PricingStructure.CAPPED_VARIABLE
    return PricingStructure.FLAT


def _classify_contract(
    product_code: str, term: int | None, is_variable: bool,
) -> ContractType:
    if term and term > 0:
        return ContractType.FIXED
    if is_variable:
        return ContractType.VARIABLE
    return ContractType.ROLLING


def _extract_tariff_from_product(
    product_data: dict,
    gsp: str,
    product_code: str,
) -> dict | None:
    """Extract the single-register electricity tariff dict for our GSP from product detail."""
    tariffs = product_data.get("single_register_electricity_tariffs") or {}
    gsp_key = f"_{gsp}"
    region_tariffs = tariffs.get(gsp_key, {})
    # Prefer direct_debit_monthly
    dd = region_tariffs.get("direct_debit_monthly") or region_tariffs.get("varying") or {}
    return dd if dd else None


def _fetch_standing_charge(product_code: str, tariff_code: str) -> float | None:
    """Fetch current standing charge for a tariff (p/day inc VAT)."""
    try:
        url = f"{OCTOPUS_BASE}/products/{product_code}/electricity-tariffs/{tariff_code}/standing-charges/"
        data = _get_json(url)
        results = data.get("results") or []
        if results:
            return float(results[0].get("value_inc_vat", 0))
    except Exception as exc:
        logger.debug("Standing charge fetch failed for %s: %s", tariff_code, exc)
    return None


def _fetch_unit_rate(product_code: str, tariff_code: str) -> float | None:
    """Fetch current flat unit rate (p/kWh inc VAT) — for fixed/variable tariffs."""
    try:
        url = f"{OCTOPUS_BASE}/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"
        data = _get_json(url)
        results = data.get("results") or []
        if results:
            return float(results[0].get("value_inc_vat", 0))
    except Exception as exc:
        logger.debug("Unit rate fetch failed for %s: %s", tariff_code, exc)
    return None


def _fetch_day_night_rates(
    product_code: str, tariff_code: str,
) -> tuple[float | None, float | None]:
    """Fetch day and night rates for dual-register / TOU tariffs."""
    day = night = None
    try:
        url = f"{OCTOPUS_BASE}/products/{product_code}/electricity-tariffs/{tariff_code}/day-unit-rates/"
        data = _get_json(url)
        results = data.get("results") or []
        if results:
            day = float(results[0].get("value_inc_vat", 0))
    except Exception:
        pass
    try:
        url = f"{OCTOPUS_BASE}/products/{product_code}/electricity-tariffs/{tariff_code}/night-unit-rates/"
        data = _get_json(url)
        results = data.get("results") or []
        if results:
            night = float(results[0].get("value_inc_vat", 0))
    except Exception:
        pass
    return day, night


# ── Public interface ─────────────────────────────────────────────────────────

def list_octopus_products(
    *,
    electricity_only: bool = True,
    brand: str = "OCTOPUS_ENERGY",
    max_products: int = 30,
) -> list[dict]:
    """Fetch the Octopus product catalogue (public, no auth).

    Returns raw product dicts with code, display_name, full_name, term, flags.
    """
    try:
        url = f"{OCTOPUS_BASE}/products/?brand={brand}&is_business=false"
        data = _get_json(url)
    except Exception as exc:
        logger.warning("Octopus product list fetch failed: %s", exc)
        return []

    products = data.get("results") or []
    # Filter to those with an available_from and not yet expired
    now = datetime.now(UTC)
    out = []
    for p in products:
        available_to = p.get("available_to")
        if available_to:
            try:
                at = datetime.fromisoformat(available_to.replace("Z", "+00:00"))
                if at < now:
                    continue
            except (ValueError, TypeError):
                pass
        out.append(p)
        if len(out) >= max_products:
            break
    return out


def get_tariff_product(product_code: str) -> TariffProduct | None:
    """Fetch full tariff detail for a single Octopus product.

    Resolves the regional tariff code, standing charges, unit rates,
    and contract policy.
    """
    gsp = _gsp_suffix()
    try:
        url = f"{OCTOPUS_BASE}/products/{product_code}/"
        data = _get_json(url)
    except Exception as exc:
        logger.warning("Product detail fetch failed for %s: %s", product_code, exc)
        return None

    display_name = data.get("display_name") or product_code
    full_name = data.get("full_name") or display_name
    description = data.get("description") or ""
    is_variable = bool(data.get("is_variable"))
    is_tracker = bool(data.get("is_tracker"))
    is_green = bool(data.get("is_green"))
    is_prepay = bool(data.get("is_prepay"))
    term = data.get("term")  # months, None for variable

    pricing = _classify_pricing(product_code, is_variable, is_tracker)
    contract_type = _classify_contract(product_code, term, is_variable)

    # Resolve tariff code for this GSP
    tariff_dict = _extract_tariff_from_product(data, gsp, product_code)
    if not tariff_dict:
        logger.debug("No single-register electricity tariff for GSP %s in %s", gsp, product_code)
        return None

    tariff_code = tariff_dict.get("code") or f"E-1R-{product_code}-{gsp}"

    # Standing charge
    standing = tariff_dict.get("standing_charge_inc_vat")
    if standing is None:
        standing = _fetch_standing_charge(product_code, tariff_code)

    # Unit rates
    unit_rate = tariff_dict.get("standard_unit_rate_inc_vat")
    day_rate = night_rate = None

    if pricing == PricingStructure.TIME_OF_USE:
        # Try dual-register tariff for day/night rates
        dual_code = tariff_code.replace(_SINGLE_REG, _DUAL_REG)
        day_rate, night_rate = _fetch_day_night_rates(product_code, dual_code)
        if day_rate is None:
            # Fall back to single-register standard rate
            if unit_rate is None:
                unit_rate = _fetch_unit_rate(product_code, tariff_code)
    elif pricing in (PricingStructure.FLAT, PricingStructure.CAPPED_VARIABLE):
        if unit_rate is None:
            unit_rate = _fetch_unit_rate(product_code, tariff_code)

    # Off-peak windows for known TOU products
    off_peak_start = off_peak_end = None
    code_upper = product_code.upper()
    if "GO" in code_upper:
        off_peak_start, off_peak_end = "00:30", "05:30"
    elif "COSY" in code_upper:
        off_peak_start, off_peak_end = "04:00", "07:00"

    # Exit fees: Octopus typically charges £0/fuel for variable, up to £30/fuel for fixes
    exit_fee = 0.0
    if contract_type == ContractType.FIXED:
        exit_fee = float(data.get("exit_fees") or 0) * 100  # API may return pounds
        if exit_fee == 0 and term and term >= 12:
            exit_fee = 0  # Octopus often has £0 exit fees even for fixes

    rates = RateSchedule(
        unit_rate_pence=float(unit_rate) if unit_rate is not None else None,
        day_rate_pence=float(day_rate) if day_rate is not None else None,
        night_rate_pence=float(night_rate) if night_rate is not None else None,
        off_peak_start=off_peak_start,
        off_peak_end=off_peak_end,
        standing_charge_pence_per_day=float(standing) if standing is not None else 0.0,
        export_rate_pence=None,  # populated separately if export tariff is configured
    )

    af = data.get("available_from")
    at = data.get("available_to")

    policy = TariffPolicy(
        contract_type=contract_type,
        contract_months=int(term) if term else None,
        exit_fee_pence=exit_fee,
        is_green=is_green,
        is_prepay=is_prepay,
        available_from=datetime.fromisoformat(af.replace("Z", "+00:00")) if af else None,
        available_to=datetime.fromisoformat(at.replace("Z", "+00:00")) if at else None,
    )

    return TariffProduct(
        product_code=product_code,
        tariff_code=tariff_code,
        display_name=display_name,
        full_name=full_name,
        provider="octopus",
        pricing=pricing,
        rates=rates,
        policy=policy,
        description=description,
    )


def get_available_tariffs(*, max_products: int = 15) -> list[TariffProduct]:
    """Fetch and resolve all currently available Octopus tariffs.

    Returns TariffProduct objects sorted by standing charge + unit rate.
    Caches nothing — call sparingly (once per comparison request).
    """
    raw_products = list_octopus_products(max_products=max_products)
    tariffs: list[TariffProduct] = []
    for rp in raw_products:
        code = rp.get("code")
        if not code:
            continue
        tp = get_tariff_product(code)
        if tp is not None:
            tariffs.append(tp)
    # Sort: cheapest flat rate first (Agile/tracker have None unit rate, sort last)
    tariffs.sort(key=lambda t: t.rates.unit_rate_pence or 999)
    return tariffs
