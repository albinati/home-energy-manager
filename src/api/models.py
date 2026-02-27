"""Pydantic models for API request/response schemas."""
from datetime import datetime
from enum import Enum
from typing import Optional, Any
from pydantic import BaseModel, Field


class OperationMode(str, Enum):
    HEATING = "heating"
    COOLING = "cooling"
    AUTO = "auto"
    FAN_ONLY = "fan_only"
    DRY = "dry"


class FoxESSWorkMode(str, Enum):
    SELF_USE = "Self Use"
    FEED_IN_PRIORITY = "Feed-in Priority"
    BACK_UP = "Back Up"
    FORCE_CHARGE = "Force charge"
    FORCE_DISCHARGE = "Force discharge"


class ActionStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    EXECUTED = "executed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class DaikinStatusResponse(BaseModel):
    device_id: str
    device_name: str
    model: str
    is_on: bool
    mode: str
    room_temp: Optional[float] = None
    target_temp: Optional[float] = None
    outdoor_temp: Optional[float] = None
    lwt: Optional[float] = None
    lwt_offset: Optional[float] = None
    tank_temp: Optional[float] = None
    tank_target: Optional[float] = None
    weather_regulation: bool


class FoxESSStatusResponse(BaseModel):
    soc: float = Field(description="Battery state of charge (%)")
    solar_power: float = Field(description="Current solar generation (kW)")
    grid_power: float = Field(description="Grid power - positive=importing, negative=exporting (kW)")
    battery_power: float = Field(description="Battery power - positive=charging, negative=discharging (kW)")
    load_power: float = Field(description="Current load consumption (kW)")
    work_mode: str
    updated_at: Optional[str] = Field(default=None, description="Last cloud API update time (UTC)")
    refresh_count_24h: Optional[int] = Field(default=None, description="Realtime API calls in last 24h")
    refresh_limit_24h: Optional[int] = Field(default=None, description="Daily API call limit (e.g. 1440)")


class PowerRequest(BaseModel):
    on: bool = Field(description="True to turn on, False to turn off")
    skip_confirmation: bool = Field(default=False, description="Skip confirmation step (use with caution)")


class TemperatureRequest(BaseModel):
    temperature: float = Field(ge=15, le=30, description="Target temperature in Celsius (15-30)")
    mode: Optional[str] = Field(default=None, description="Operation mode (uses current if not specified)")


class LWTOffsetRequest(BaseModel):
    offset: float = Field(ge=-10, le=10, description="Leaving water temperature offset (-10 to +10)")
    mode: Optional[str] = Field(default=None, description="Operation mode (uses current if not specified)")


class ModeRequest(BaseModel):
    mode: OperationMode = Field(description="Operation mode to set")


class TankTemperatureRequest(BaseModel):
    temperature: float = Field(ge=30, le=60, description="DHW tank target temperature (30-60°C)")


class TankPowerRequest(BaseModel):
    on: bool = Field(description="True to turn on, False to turn off")
    skip_confirmation: bool = Field(default=False, description="Skip confirmation step")


class FoxESSModeRequest(BaseModel):
    mode: FoxESSWorkMode = Field(description="Inverter work mode")
    skip_confirmation: bool = Field(default=False, description="Skip confirmation step")


class ChargePeriodRequest(BaseModel):
    start_time: str = Field(pattern=r"^\d{2}:\d{2}$", description="Start time (HH:MM)")
    end_time: str = Field(pattern=r"^\d{2}:\d{2}$", description="End time (HH:MM)")
    target_soc: int = Field(ge=10, le=100, description="Target state of charge (%)")
    period_index: int = Field(ge=0, le=1, default=0, description="Period slot (0 or 1)")


class PendingAction(BaseModel):
    action_id: str
    action_type: str
    description: str
    parameters: dict[str, Any]
    expires_at: datetime
    status: ActionStatus = ActionStatus.PENDING


class PendingActionResponse(BaseModel):
    requires_confirmation: bool = True
    action: PendingAction
    message: str = Field(description="Human-readable confirmation prompt")


class ConfirmRequest(BaseModel):
    confirmed: bool = Field(description="True to confirm and execute, False to cancel")


class ActionResult(BaseModel):
    success: bool
    message: str
    action_id: Optional[str] = None
    data: Optional[dict[str, Any]] = None


class OpenClawAction(str, Enum):
    DAIKIN_POWER = "daikin.power"
    DAIKIN_TEMPERATURE = "daikin.temperature"
    DAIKIN_LWT_OFFSET = "daikin.lwt_offset"
    DAIKIN_MODE = "daikin.mode"
    DAIKIN_TANK_TEMPERATURE = "daikin.tank_temperature"
    DAIKIN_TANK_POWER = "daikin.tank_power"
    FOXESS_MODE = "foxess.mode"
    FOXESS_CHARGE_PERIOD = "foxess.charge_period"


class OpenClawExecuteRequest(BaseModel):
    action: OpenClawAction
    parameters: dict[str, Any]
    confirmation_token: Optional[str] = Field(default=None, description="Token from previous pending action")


class OpenClawCapability(BaseModel):
    action: str
    description: str
    parameters: dict[str, Any]
    requires_confirmation: bool
    safeguards: list[str]


class OpenClawCapabilitiesResponse(BaseModel):
    capabilities: list[OpenClawCapability]


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None


# Energy provider models

class EnergyProviderEnum(str, Enum):
    OCTOPUS = "octopus"
    BRITISH_GAS = "british_gas"
    MANUAL = "manual"


class TariffTypeEnum(str, Enum):
    FIXED = "fixed"
    VARIABLE = "variable"
    AGILE = "agile"
    GO = "go"
    TRACKER = "tracker"
    ECONOMY_7 = "economy_7"
    FLUX = "flux"


class EnergyProviderInfo(BaseModel):
    provider: EnergyProviderEnum
    name: str
    is_configured: bool
    description: str


class EnergyProvidersResponse(BaseModel):
    providers: list[EnergyProviderInfo]
    configured_count: int = Field(description="Number of configured providers")


class TariffResponse(BaseModel):
    provider: EnergyProviderEnum
    tariff_name: str
    tariff_type: TariffTypeEnum
    import_rate: float = Field(description="Import rate in p/kWh")
    export_rate: Optional[float] = Field(default=None, description="Export rate in p/kWh")
    standing_charge: Optional[float] = Field(default=None, description="Standing charge in p/day")
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    is_peak: bool = False
    next_rate: Optional[float] = Field(default=None, description="Next rate in p/kWh (for agile tariffs)")
    next_rate_from: Optional[datetime] = None


class EnergyUsageResponse(BaseModel):
    period_start: datetime
    period_end: datetime
    import_kwh: float = Field(description="Total imported energy in kWh")
    export_kwh: float = Field(description="Total exported energy in kWh")
    import_cost_pence: float = Field(description="Import cost in pence")
    export_earnings_pence: float = Field(description="Export earnings in pence")
    standing_charge_pence: float = Field(description="Standing charge total in pence")
    net_cost_pence: float = Field(description="Net cost in pence")
    net_cost_pounds: float = Field(description="Net cost in pounds")


class MonthlyEnergySummaryResponse(BaseModel):
    year: int
    month: int
    month_str: str
    import_kwh: float = 0.0
    export_kwh: float = 0.0
    solar_kwh: float = 0.0
    load_kwh: float = 0.0
    charge_kwh: float = 0.0
    discharge_kwh: float = 0.0


class MonthlyCostSummaryResponse(BaseModel):
    import_cost_pence: float = 0.0
    export_earnings_pence: float = 0.0
    standing_charge_pence: float = 0.0
    net_cost_pence: float = 0.0
    net_cost_pounds: float = 0.0
    import_cost_pounds: float = 0.0
    export_earnings_pounds: float = 0.0


class MonthlyInsightsResponse(BaseModel):
    energy: MonthlyEnergySummaryResponse
    cost: MonthlyCostSummaryResponse
    heating_estimate_kwh: Optional[float] = None
    heating_estimate_cost_pence: Optional[float] = None
    equivalent_gas_cost_pence: Optional[float] = None
    equivalent_gas_cost_pounds: Optional[float] = None
    gas_comparison_ahead_pounds: Optional[float] = None


class ChartDataPoint(BaseModel):
    date: str
    import_kwh: float = 0.0
    export_kwh: float = 0.0
    solar_kwh: float = 0.0
    load_kwh: float = 0.0
    charge_kwh: float = 0.0
    discharge_kwh: float = 0.0


class TempBandSummaryResponse(BaseModel):
    band: str
    days: int
    heating_kwh: float
    cost_pounds: float
    avg_temp_c: Optional[float] = None


class HeatingAnalyticsResponse(BaseModel):
    heating_percent_of_cost: Optional[float] = None
    heating_percent_of_consumption: Optional[float] = None
    avg_outdoor_temp_c: Optional[float] = None
    degree_days: Optional[float] = None
    cost_per_degree_day_pounds: Optional[float] = None
    heating_kwh_per_degree_day: Optional[float] = None
    temp_bands: list[TempBandSummaryResponse] = []


class PeriodInsightsResponse(BaseModel):
    period: str  # day | week | month | year
    period_label: str
    energy: MonthlyEnergySummaryResponse
    cost: MonthlyCostSummaryResponse
    heating_estimate_kwh: Optional[float] = None
    heating_estimate_cost_pence: Optional[float] = None
    equivalent_gas_cost_pence: Optional[float] = None
    equivalent_gas_cost_pounds: Optional[float] = None
    gas_comparison_ahead_pounds: Optional[float] = None
    chart_data: list[ChartDataPoint] = []
    heating_analytics: Optional[HeatingAnalyticsResponse] = None


class EnergyReportResponse(PeriodInsightsResponse):
    """Full data report: same as period insights plus a short narrative for OpenClaw/voice."""

    summary: str = Field(
        default="",
        description="Short narrative summary for OpenClaw (cost, balance, gas comparison). Use for TTS or chat.",
    )


class EnergyInsightsTextResponse(BaseModel):
    summary: str = Field(description="Short narrative for OpenClaw (this month cost, equivalent gas)")


# AI Assistant models

class AssistantPreference(str, Enum):
    COMFORT = "comfort"
    BALANCED = "balanced"
    SAVINGS = "savings"


class AssistantRecommendRequest(BaseModel):
    message: Optional[str] = Field(default=None, description="Optional user message or request")
    preference: AssistantPreference = Field(description="Comfort vs cost balance")


class SuggestedActionSchema(BaseModel):
    action: str = Field(description="Action type (e.g. daikin.temperature)")
    parameters: dict[str, Any] = Field(description="Action parameters")
    reason: Optional[str] = Field(default=None, description="Short reason for the suggestion")


class AssistantRecommendResponse(BaseModel):
    reply: str = Field(description="Assistant reply text")
    suggested_actions: list[SuggestedActionSchema] = Field(description="List of suggested actions")


class AssistantApplyActionRequest(BaseModel):
    action: str = Field(description="Action type")
    parameters: dict[str, Any] = Field(description="Action parameters")


class AssistantApplyRequest(BaseModel):
    actions: list[AssistantApplyActionRequest] = Field(description="Actions to apply (from recommend response)")


class AssistantApplyResultItem(BaseModel):
    action_type: str
    success: bool
    message: str
    requires_confirmation: bool = False
    confirmation_token: Optional[str] = None
    action_id: Optional[str] = None


class AssistantApplyResponse(BaseModel):
    results: list[AssistantApplyResultItem] = Field(description="Per-action results")


class SchedulerStatusResponse(BaseModel):
    """Agile scheduler status: current price, next cheap window, planned LWT adjustment."""
    enabled: bool
    paused: bool
    current_price_pence: Optional[float] = None
    next_cheap_from: Optional[str] = None
    next_cheap_to: Optional[str] = None
    planned_lwt_adjustment: float = 0.0
    tariff_code: Optional[str] = None
