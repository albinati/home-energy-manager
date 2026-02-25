"""Data models for energy provider integrations."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


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
    export_rate: Optional[float] = None  # p/kWh (SEG/export tariff)
    standing_charge: Optional[float] = None  # p/day
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    is_peak: bool = False
    next_rate: Optional[float] = None
    next_rate_from: Optional[datetime] = None


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
    api_key: Optional[str] = None
    account_number: Optional[str] = None
    mpan: Optional[str] = None  # Meter Point Administration Number
    mprn: Optional[str] = None  # Meter Point Reference Number (gas)
    is_configured: bool = False
