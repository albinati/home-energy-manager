"""Stub energy provider listing and tariff/usage endpoints (Octopus integration TBD)."""

from fastapi import APIRouter, HTTPException

from ...config import config
from ..models import (
    EnergyProviderEnum,
    EnergyProviderInfo,
    EnergyProvidersResponse,
    EnergyUsageResponse,
    TariffResponse,
    TariffTypeEnum,
)

router = APIRouter(prefix="/api/v1/energy", tags=["energy"])


def _is_manual_tariff_configured() -> bool:
    return config.MANUAL_TARIFF_IMPORT_PENCE > 0 or config.MANUAL_TARIFF_EXPORT_PENCE > 0


ENERGY_PROVIDERS = [
    EnergyProviderInfo(
        provider=EnergyProviderEnum.OCTOPUS,
        name="Octopus Energy",
        is_configured=bool(config.OCTOPUS_API_KEY),
        description="Agile, Go, Tracker, and fixed tariffs with half-hourly pricing data",
    ),
    EnergyProviderInfo(
        provider=EnergyProviderEnum.BRITISH_GAS,
        name="British Gas",
        is_configured=False,
        description="Reserved — live API integration not implemented yet",
    ),
    EnergyProviderInfo(
        provider=EnergyProviderEnum.MANUAL,
        name="Manual Entry",
        is_configured=_is_manual_tariff_configured(),
        description="Manually enter your tariff rates for cost tracking",
    ),
]


@router.get("/providers", response_model=EnergyProvidersResponse)
async def energy_providers():
    """List available energy providers and their configuration status."""
    configured = sum(1 for p in ENERGY_PROVIDERS if p.is_configured)
    return EnergyProvidersResponse(
        providers=ENERGY_PROVIDERS,
        configured_count=configured,
    )


@router.get("/tariff", response_model=TariffResponse)
async def energy_tariff():
    """Get current tariff information from configured energy provider.

    Uses manual tariff (MANUAL_TARIFF_IMPORT_PENCE / MANUAL_TARIFF_EXPORT_PENCE) when set.
    Returns 503 if no provider and no manual tariff configured.
    """
    if _is_manual_tariff_configured():
        return TariffResponse(
            provider=EnergyProviderEnum.MANUAL,
            tariff_name="Manual",
            tariff_type=TariffTypeEnum.FIXED,
            import_rate=config.MANUAL_TARIFF_IMPORT_PENCE,
            export_rate=config.MANUAL_TARIFF_EXPORT_PENCE if config.MANUAL_TARIFF_EXPORT_PENCE > 0 else None,
        )
    configured = [p for p in ENERGY_PROVIDERS if p.is_configured]
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="No energy provider configured. Set OCTOPUS_API_KEY or MANUAL_TARIFF_IMPORT_PENCE in .env",
        )
    raise HTTPException(
        status_code=501,
        detail="Energy provider integration not yet implemented. Coming soon!",
    )


@router.get("/usage", response_model=EnergyUsageResponse)
async def energy_usage():
    """Get energy usage and cost summary.

    Returns 503 if no energy provider is configured.
    """
    configured = [p for p in ENERGY_PROVIDERS if p.is_configured]
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="No energy provider configured. Set OCTOPUS_API_KEY in .env",
        )
    raise HTTPException(
        status_code=501,
        detail="Energy provider integration not yet implemented. Coming soon!",
    )
