"""Abstract base class for energy provider clients."""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from .models import TariffInfo, EnergyUsageSummary, EnergyProvider


class EnergyProviderError(Exception):
    """Base exception for energy provider errors."""
    pass


class EnergyProviderNotConfigured(EnergyProviderError):
    """Raised when the provider is not configured."""
    pass


class EnergyProviderClient(ABC):
    """Abstract base class for energy provider API clients.
    
    Implementations should be created for each supported provider:
    - OctopusEnergyClient
    - BritishGasClient
    - ManualTariffClient (for manual rate entry)
    """
    
    @property
    @abstractmethod
    def provider(self) -> EnergyProvider:
        """Return the provider type."""
        pass
    
    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """Check if the provider is properly configured."""
        pass
    
    @abstractmethod
    def get_current_tariff(self) -> TariffInfo:
        """Get the current tariff information.
        
        Returns:
            TariffInfo with current import/export rates
            
        Raises:
            EnergyProviderNotConfigured: If credentials are missing
            EnergyProviderError: If API call fails
        """
        pass
    
    @abstractmethod
    def get_usage(
        self,
        start: datetime,
        end: datetime,
    ) -> EnergyUsageSummary:
        """Get energy usage and costs for a period.
        
        Args:
            start: Start of the period
            end: End of the period
            
        Returns:
            EnergyUsageSummary with usage and cost data
            
        Raises:
            EnergyProviderNotConfigured: If credentials are missing
            EnergyProviderError: If API call fails
        """
        pass
    
    def get_today_usage(self) -> EnergyUsageSummary:
        """Get today's energy usage and costs.
        
        Convenience method that calls get_usage for today.
        """
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        now = datetime.now()
        return self.get_usage(today, now)
    
    def get_rates_ahead(self, hours: int = 24) -> list[TariffInfo]:
        """Get upcoming rates for agile/variable tariffs.
        
        Default implementation returns empty list.
        Override for providers that support ahead-of-time rate data.
        
        Args:
            hours: Number of hours to look ahead
            
        Returns:
            List of TariffInfo for upcoming periods
        """
        return []


class ManualTariffClient(EnergyProviderClient):
    """Client for manually configured tariff rates.
    
    Use this when you don't have API access to your energy provider
    but want to track costs based on known rates.
    """
    
    def __init__(
        self,
        import_rate: float,
        export_rate: Optional[float] = None,
        standing_charge: Optional[float] = None,
        tariff_name: str = "Manual Tariff",
    ):
        self._import_rate = import_rate
        self._export_rate = export_rate
        self._standing_charge = standing_charge
        self._tariff_name = tariff_name
    
    @property
    def provider(self) -> EnergyProvider:
        return EnergyProvider.MANUAL
    
    @property
    def is_configured(self) -> bool:
        return self._import_rate > 0
    
    def get_current_tariff(self) -> TariffInfo:
        from .models import TariffType
        return TariffInfo(
            provider=EnergyProvider.MANUAL,
            tariff_name=self._tariff_name,
            tariff_type=TariffType.FIXED,
            import_rate=self._import_rate,
            export_rate=self._export_rate,
            standing_charge=self._standing_charge,
        )
    
    def get_usage(self, start: datetime, end: datetime) -> EnergyUsageSummary:
        raise EnergyProviderError(
            "Manual tariff client cannot fetch usage data. "
            "Use Fox ESS data combined with manual rates instead."
        )
