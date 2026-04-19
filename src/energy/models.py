"""Data models for energy provider integrations."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class EnergyProvider(str, Enum):
    """Supported energy providers."""
    OCTOPUS = "octopus"
    BRITISH_GAS = "british_gas"
    MANUAL = "manual"


class TariffType(str, Enum):
    """Types of energy tariffs."""
    FIXED = "fixed"
    VARIABLE = "variable"
    AGILE = "agile"
    GO = "go"
    TRACKER = "tracker"
    ECONOMY_7 = "economy_7"
    FLUX = "flux"


@dataclass
class TariffInfo:
    """Current tariff information from an energy provider."""
    provider: EnergyProvider
    tariff_name: str
    tariff_type: TariffType
    import_rate: float  # p/kWh
    export_rate: float | None = None  # p/kWh (SEG/export tariff)
    standing_charge: float | None = None  # p/day
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    is_peak: bool = False
    next_rate: float | None = None
    next_rate_from: datetime | None = None


@dataclass
class EnergyUsageSummary:
    """Energy usage and cost summary for a period."""
    period_start: datetime
    period_end: datetime
    import_kwh: float = 0.0
    export_kwh: float = 0.0
    import_cost: float = 0.0  # in pence
    export_earnings: float = 0.0  # in pence
    standing_charge_total: float = 0.0  # in pence
    net_cost: float = 0.0  # in pence (import_cost + standing - export_earnings)
    
    @property
    def net_cost_pounds(self) -> float:
        """Net cost in pounds."""
        return self.net_cost / 100
    
    @property
    def import_cost_pounds(self) -> float:
        """Import cost in pounds."""
        return self.import_cost / 100
    
    @property
    def export_earnings_pounds(self) -> float:
        """Export earnings in pounds."""
        return self.export_earnings / 100


@dataclass
class ProviderConfig:
    """Configuration for an energy provider connection."""
    provider: EnergyProvider
    api_key: str | None = None
    account_number: str | None = None
    mpan: str | None = None  # Meter Point Administration Number
    mprn: str | None = None  # Meter Point Reference Number (gas)
    is_configured: bool = False
