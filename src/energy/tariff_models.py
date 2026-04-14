"""Domain models for tariff comparison and simulation.

A TariffProduct represents a single energy product (Agile, Go, Flexible, Tracker,
fixed, etc.) with its pricing structure, standing charges, and contract policy.

A TariffSimulationResult represents the projected cost of running the household
on that tariff for a given period, using actual Fox ESS import/export kWh data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class PricingStructure(str, Enum):
    """How the unit rate is determined."""
    FLAT = "flat"               # single rate, all hours
    TIME_OF_USE = "time_of_use" # day/night or multi-register (Economy 7, Go)
    HALF_HOURLY = "half_hourly" # Agile: different rate every 30 min
    TRACKER = "tracker"         # follows wholesale + fixed markup
    CAPPED_VARIABLE = "capped_variable"  # standard variable with Ofgem cap


class ContractType(str, Enum):
    FIXED = "fixed"       # locked rate for N months
    VARIABLE = "variable" # rate can change; no lock-in
    ROLLING = "rolling"   # month-to-month, cancel anytime


@dataclass
class TariffPolicy:
    """Contract terms and conditions that affect the true cost of switching."""
    contract_type: ContractType = ContractType.VARIABLE
    contract_months: Optional[int] = None      # e.g. 12 for a 12-month fix
    exit_fee_pence: float = 0.0                # early termination fee (pence per fuel)
    exit_fee_per_fuel: bool = True             # True = fee applies per fuel (gas+elec)
    available_from: Optional[datetime] = None
    available_to: Optional[datetime] = None    # None = currently available
    is_green: bool = False
    is_prepay: bool = False
    payment_method: str = "direct_debit_monthly"


@dataclass
class RateSchedule:
    """Rate structure for a tariff. For flat tariffs only unit_rate_pence is needed.
    For time-of-use, day/night rates + off-peak windows are provided.
    For half-hourly (Agile), rates come from the separate rates API per slot.
    """
    unit_rate_pence: Optional[float] = None          # flat rate (p/kWh inc VAT)
    day_rate_pence: Optional[float] = None            # day/peak rate for TOU
    night_rate_pence: Optional[float] = None          # night/off-peak rate for TOU
    off_peak_start: Optional[str] = None              # e.g. "00:30" for Go/Eco7
    off_peak_end: Optional[str] = None                # e.g. "05:30"
    standing_charge_pence_per_day: float = 0.0        # inc VAT
    export_rate_pence: Optional[float] = None         # SEG / export payment (p/kWh)


@dataclass
class TariffProduct:
    """A complete tariff product available for comparison."""
    product_code: str                 # e.g. "AGILE-24-10-01"
    tariff_code: str                  # e.g. "E-1R-AGILE-24-10-01-C" (region-specific)
    display_name: str                 # e.g. "Agile Octopus"
    full_name: str                    # e.g. "Agile Octopus October 2024 v1"
    provider: str = "octopus"
    pricing: PricingStructure = PricingStructure.FLAT
    rates: RateSchedule = field(default_factory=RateSchedule)
    policy: TariffPolicy = field(default_factory=TariffPolicy)
    description: str = ""

    @property
    def annual_standing_charge_pounds(self) -> float:
        return self.rates.standing_charge_pence_per_day * 365 / 100

    @property
    def has_export(self) -> bool:
        return self.rates.export_rate_pence is not None and self.rates.export_rate_pence > 0

    def summary_line(self) -> str:
        """One-line human-readable summary for OpenClaw / API."""
        rate_str = ""
        if self.pricing == PricingStructure.FLAT and self.rates.unit_rate_pence is not None:
            rate_str = f"{self.rates.unit_rate_pence:.2f}p/kWh"
        elif self.pricing == PricingStructure.TIME_OF_USE:
            rate_str = (
                f"day {self.rates.day_rate_pence:.2f}p / night {self.rates.night_rate_pence:.2f}p"
            )
        elif self.pricing == PricingStructure.HALF_HOURLY:
            rate_str = "half-hourly variable"
        elif self.pricing == PricingStructure.TRACKER:
            rate_str = "wholesale tracker"
        else:
            rate_str = "variable"
        standing = f"{self.rates.standing_charge_pence_per_day:.2f}p/day"
        lock = ""
        if self.policy.contract_type == ContractType.FIXED and self.policy.contract_months:
            lock = f", {self.policy.contract_months}mo fix"
            if self.policy.exit_fee_pence > 0:
                lock += f" (£{self.policy.exit_fee_pence / 100:.0f} exit fee)"
        return f"{self.display_name}: {rate_str}, standing {standing}{lock}"


@dataclass
class TariffSimulationResult:
    """Projected cost of running the household on a candidate tariff."""
    tariff: TariffProduct
    period_days: int
    import_kwh: float
    export_kwh: float
    import_cost_pence: float
    export_earnings_pence: float
    standing_charge_pence: float
    net_cost_pence: float        # import + standing - export

    # Annualised for like-for-like comparison
    annual_net_cost_pounds: float = 0.0
    annual_import_cost_pounds: float = 0.0
    annual_standing_charge_pounds: float = 0.0
    annual_export_earnings_pounds: float = 0.0

    # Policy impact
    exit_fee_pounds: float = 0.0
    lock_in_months: Optional[int] = None
    first_year_effective_cost_pounds: float = 0.0  # annual net + exit fee if leaving early

    notes: str = ""

    @property
    def net_cost_pounds(self) -> float:
        return self.net_cost_pence / 100

    @property
    def daily_cost_pence(self) -> float:
        return self.net_cost_pence / max(1, self.period_days)


@dataclass
class TariffRecommendation:
    """Ranked tariff recommendation output."""
    current_tariff: Optional[TariffSimulationResult] = None
    candidates: list[TariffSimulationResult] = field(default_factory=list)
    best: Optional[TariffSimulationResult] = None
    savings_vs_current_pounds: Optional[float] = None
    summary: str = ""
    generated_at: Optional[datetime] = None
