"""SVT and fixed-tariff shadow rates for PnL comparison."""
from __future__ import annotations

from ..config import config


def svt_rate_pence() -> float:
    return float(config.SVT_RATE_PENCE)


def fixed_shadow_rate_pence() -> float:
    v = float(config.MANUAL_TARIFF_IMPORT_PENCE or 0)
    return v if v > 0 else svt_rate_pence()
