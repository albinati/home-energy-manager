// API response shapes consumed by the SPA.
// These mirror the FastAPI handlers and runtime_settings.SCHEMA — when those
// change, update here. Keep this file flat (no inheritance) for grep-ability.

export type SettingType = "int" | "float" | "bool" | "str" | "enum";

export interface SettingSpec {
  key: string;
  value: string | number | boolean;
  default: string | number | boolean;
  type: SettingType;
  min?: number | null;
  max?: number | null;
  enum?: string[] | null;
  description: string;
  cron_reload?: boolean;
  overridden: boolean;
  updated_at?: string | null;
}

export interface SettingsList {
  settings: SettingSpec[];
}

export interface SimulateBatchResponse {
  simulation_id: string;
  diffs: Array<{
    key: string;
    current: unknown;
    proposed: unknown;
    cron_reload?: boolean;
  }>;
  warnings: string[];
}

export interface ApplyBatchResponse {
  applied: Array<{ key: string; value: unknown }>;
  errors?: Array<{ key: string; error: string }>;
}

/* ----- /cockpit/now ----- */

export interface CockpitState {
  soc_pct: number;
  soc_kwh: number;
  solar_kw: number;
  load_kw: number;
  grid_kw: number;       // positive = importing; negative = exporting
  battery_kw: number;    // positive = charging
  tank_c: number | null;
  indoor_c: number | null;
  lwt_c: number | null;
  daikin_mode: string | null;
}

export interface CockpitSlot {
  slot_time_utc?: string;
  price_import_p: number;
  price_export_p: number;
  fox_mode: string;
  kind?: string; // cheap | standard | peak | peak_export | negative | solar_charge
}

export interface CockpitTransition {
  t_utc: string;
  new_fox_mode: string;
  kind?: string;
}

export interface FreshnessEntry {
  age_s: number;
  fetched_at_utc: string | null;
  stale: boolean;
}

export interface CockpitNow {
  now_utc: string;
  state: CockpitState;
  current_slot: CockpitSlot;
  next_transition: CockpitTransition | null;
  freshness?: Record<string, FreshnessEntry>;
  thresholds?: { cheap_p: number; peak_p: number };
}

/* ----- /scheduler/timeline ----- */

export interface TimelineSlot {
  slot_time_utc: string;
  fox_mode?: string;
  lp_kind?: string;
  dispatched_kind?: string;
  price_import_p?: number;
  price_export_p?: number;
  soc_percent?: number | null;
  consumption_kwh?: number | null;
  reason?: string | null;
}

export interface SchedulerTimeline {
  run_id?: number | null;
  run_at?: string | null;
  plan_date?: string | null;
  tariff_code?: string | null;
  peak_threshold_pence?: number;
  cheap_threshold_pence?: number;
  executed: TimelineSlot[];
  ongoing?: TimelineSlot | null;
  planned: TimelineSlot[];
}

/* ----- /optimization/decisions/{run_id} ----- */

export interface DispatchDecision {
  slot_time_utc: string;
  lp_kind: string;
  dispatched_kind: string;
  committed: boolean;
  reason?: string | null;
  scen_optimistic_exp_kwh?: number;
  scen_nominal_exp_kwh?: number;
  scen_pessimistic_exp_kwh?: number;
}

export interface DispatchDecisionsResponse {
  run_id: number;
  decisions: DispatchDecision[];
  summary?: {
    total_slots: number;
    peak_export_committed: number;
    peak_export_dropped: number;
    drop_reasons?: Record<string, number>;
  };
}

/* ----- /weather + /execution/today + /agile/today ----- */

export interface WeatherSlot {
  time: string;          // ISO datetime
  temp_c: number;
  pv_kw: number;
}

export interface WeatherResponse {
  forecast: WeatherSlot[];
  daikin?: {
    room_temp: number | null;
    outdoor_temp: number | null;
    lwt: number | null;
    tank_temp: number | null;
    error?: string;
  };
}

// Real /execution/today shape — per 30-min slot with rich realised data:
// price paid, energy split (consumption / daikin / residual), realised cost
// vs SVT shadow, Daikin sensor temps.
export interface ExecutionSlot {
  slot_utc: string;
  slot_kind?: string;
  agile_p?: number | null;
  consumption_kwh?: number | null;
  daikin_kwh_est?: number | null;
  residual_kwh?: number | null;
  cost_realised_p?: number | null;
  cost_daikin_p?: number | null;
  cost_residual_p?: number | null;
  cost_svt_p?: number | null;
  delta_vs_svt_p?: number | null;
  soc_percent?: number | null;
  fox_mode?: string | null;
  daikin_outdoor_c?: number | null;
  daikin_lwt_c?: number | null;
}

export interface ExecutionTodayResponse {
  date: string;
  data_quality_note?: string;
  slots: ExecutionSlot[];
  totals?: Record<string, number>;
}

// /agile/today response — both directions, with a current-slot price.
export interface AgileSlot {
  valid_from: string;
  valid_to: string;
  p: number;
  kind?: string; // negative | cheap | standard | peak (server classification)
}

export interface AgileTodayResponse {
  tariff_import_code: string;
  tariff_export_code: string;
  import_slots: AgileSlot[];
  export_slots: AgileSlot[];
  current_import_p?: number;
  current_export_p?: number;
  now_utc?: string;
}

// /agile/day?date=YYYY-MM-DD — single direction (default import).
export interface AgileDaySlotsResponse {
  date: string;
  tariff_code: string;
  tz?: string;
  slots: AgileSlot[];
}

// /octopus/consumption — half-hour slots from the smart meter (cached).
export interface OctopusConsumptionSlot {
  interval_start: string;    // ISO with offset
  interval_end: string;
  consumption_kwh: number;
}

export interface OctopusConsumptionResponse {
  ok: boolean;
  error?: string | null;
  mpan?: string;
  serial?: string;
  slots: OctopusConsumptionSlot[];
}

/* ----- /patterns/pv-calibration ----- */

export interface PvCalibration {
  window_days: number;
  factor: number;
  last_updated?: string;
  confidence?: string;
}

/* ----- /attribution/day ----- */

export interface AttributionDay {
  date: string;
  solar_kwh: number;
  load_kwh: number;
  import_kwh: number;
  export_kwh: number;
  charge_kwh: number;
  discharge_kwh: number;
  shares?: {
    self_use_pct: number;
    battery_pct: number;
    export_pct: number;
  };
}

/* ----- /energy/report (day or month) ----- */
// Real shape — flat energy + cost objects, no nested pnl. /energy/report
// defaults to period=month; pass period=day for a single day rollup.
export interface EnergyReport {
  period?: "day" | "month" | string;
  period_label?: string;
  energy?: {
    year?: number;
    month?: number;
    month_str?: string;
    import_kwh: number;
    export_kwh: number;
    solar_kwh: number;
    load_kwh: number;
    charge_kwh: number;
    discharge_kwh: number;
  };
  cost?: {
    import_cost_pence: number;
    export_earnings_pence: number;
    standing_charge_pence: number;
    net_cost_pence: number;
    net_cost_pounds: number;
    import_cost_pounds: number;
    export_earnings_pounds: number;
  };
  heating_estimate_kwh?: number | null;
  heating_estimate_cost_pence?: number | null;
  equivalent_gas_cost_pence?: number | null;
  equivalent_gas_cost_pounds?: number | null;
  gas_comparison_ahead_pounds?: number | null;
  chart_data?: Array<{
    date: string;
    import_kwh: number;
    export_kwh: number;
    solar_kwh: number;
    load_kwh: number;
    charge_kwh: number;
    discharge_kwh: number;
  }>;
  heating_analytics?: Record<string, unknown>;
}

// Actual /energy/monthly response shape. The endpoint nests energy + cost.
// No savings_vs_svt is exposed here — for that we'd call /energy/report per
// month or sum /metrics.pnl over time.
export interface MonthlyEnergyEnergy {
  year: number;
  month: number;
  month_str: string;       // "YYYY-MM"
  import_kwh: number;
  export_kwh: number;
  solar_kwh: number;
  load_kwh: number;
  charge_kwh: number;
  discharge_kwh: number;
}
export interface MonthlyEnergyCost {
  import_cost_pence: number;
  export_earnings_pence: number;
  standing_charge_pence: number;
  net_cost_pence: number;
  net_cost_pounds: number;
  import_cost_pounds: number;
  export_earnings_pounds: number;
}
export interface MonthlyEnergy {
  energy: MonthlyEnergyEnergy;
  cost: MonthlyEnergyCost;
  heating_estimate_kwh?: number | null;
  heating_estimate_cost_pence?: number | null;
  equivalent_gas_cost_pence?: number | null;
  equivalent_gas_cost_pounds?: number | null;
  gas_comparison_ahead_pounds?: number | null;
}

/* ----- /energy/period — drill-down (day/week/month/year) ----- */

// Same chart_data point shape across all granularities — granularity is
// indicated by the wrapping `period` field on the response.
export interface PeriodChartPoint {
  date: string;       // YYYY-MM-DD for day/week/month; YYYY-MM-01 for year
  import_kwh: number;
  export_kwh: number;
  solar_kwh: number;
  load_kwh: number;
  charge_kwh: number;
  discharge_kwh: number;
}

export interface PeriodInsightsResponse {
  period: "day" | "week" | "month" | "year" | string;
  period_label: string;
  energy: MonthlyEnergyEnergy;
  cost: MonthlyEnergyCost;
  chart_data: PeriodChartPoint[];
  heating_estimate_kwh?: number | null;
  heating_estimate_cost_pence?: number | null;
  equivalent_gas_cost_pence?: number | null;
  equivalent_gas_cost_pounds?: number | null;
  gas_comparison_ahead_pounds?: number | null;
}

/* ----- /daikin/consumption — Onecta-measured actuals ----- */

export interface DaikinConsumptionBucket {
  when: string;                     // ISO timestamp (day=YYYY-MM-DDTHH:00, other=YYYY-MM-DD)
  bucket_idx?: number;              // 0-11 for day period
  kwh_total: number | null;
  kwh_heating: number | null;
  kwh_dhw: number | null;
  cop?: number | null;              // weekly/monthly only
  source?: string | null;
}

export interface DaikinConsumptionResponse {
  period: "day" | "week" | "month" | "year" | string;
  label: string;
  buckets: DaikinConsumptionBucket[];
  totals: {
    kwh_total: number;
    kwh_heating: number;
    kwh_dhw: number;
    dhw_share_pct?: number | null;
  };
  source?: string;
}

/* ----- /metrics (~1s, summary KPIs) ----- */

// /daikin/status — cached when refresh=false (default)
export interface DaikinDevice {
  device_id: string;
  device_name?: string;
  mode?: string | null;
  room_temp?: number | null;
  target_temp?: number | null;
  tank_temp?: number | null;
  tank_target?: number | null;
  outdoor_temp?: number | null;
  lwt?: number | null;
  weather_regulation?: boolean | null;
  control_mode?: string | null;
  state_summary?: string | null;
  is_on?: boolean | null;
  tank_power?: boolean | null;
}

// /daikin/quota + /foxess/quota — shared shape
export interface ApiQuotaResponse {
  cache_age_seconds?: number | null;
  cache_warm?: boolean;
  stale?: boolean;
  last_refresh_at_utc?: string | null;
  last_updated_epoch?: number;
  refresh_count_24h?: number;
  // Rolling 24h — drives the local soft cap; can exceed budget during retry storms.
  quota_used_24h?: number;
  quota_remaining_24h?: number;
  // Since midnight UTC — matches what Daikin/Fox actually enforce. Optional;
  // older backends won't return it (UI falls back to quota_used_24h).
  quota_used_today_utc?: number | null;
  quota_remaining_today_utc?: number | null;
  // Failed calls in the rolling 24h — surfaces retry-loop incidents.
  quota_failed_24h?: number | null;
  daily_budget?: number;
  blocked?: boolean;
  last_blocked_at?: number | null;
}

/* ----- /tariffs/dashboard (POST) — comparison vs Octopus catalogue ----- */

export interface TariffTotalRow {
  product_code: string;
  display_name: string;
  pricing: "half_hourly" | "time_of_use" | "flat" | string;
  total_pence: number;
  daily_avg_pence: number;
  annual_pounds: number;
  standing_per_day: number;
  unit_rate_pence: number;
  contract_type?: string;
  contract_months?: number;
  exit_fee_pounds?: number;
  is_green?: boolean;
  wins?: number;
  is_current?: boolean;
  savings_vs_current_pounds: number;
}

export interface TariffDashboardUsage {
  total_import_kwh: number;
  total_export_kwh: number;
  total_days: number;
}

export interface TariffDashboardResponse {
  ok: boolean;
  error?: string | null;
  granularity?: "daily" | "weekly" | "monthly" | string;
  periods?: Array<{ label: string; import_kwh: number; export_kwh: number; days: number; costs: Record<string, number>; winner: string }>;
  totals?: TariffTotalRow[];
  current_product_code?: string;
  current_annual_pounds?: number;
  usage?: TariffDashboardUsage;
  data_source?: string;
}

export interface MetricsResponse {
  pnl?: {
    daily?: { delta_vs_svt_pounds?: number; delta_vs_fixed_pounds?: number };
    weekly?: { delta_vs_svt_pounds?: number };
    monthly?: { delta_vs_svt_pounds?: number };
  };
  target_vwap_pence?: number;
  realised_vwap_pence?: number;
  slippage_pence?: number;
  arbitrage_efficiency_pct?: number;
  peak_import_pct?: number;
  off_peak_import_pct?: number;
  battery_soc_percent?: number;
  battery_capacity_kwh?: number;
  octopus_fetch?: {
    last_success_at?: string | null;
    consecutive_failures?: number;
    survival_mode_since?: string | null;
  };
  sla?: {
    actions_executed_on_time_pct?: number;
    safe_default_restored_pct?: number;
    optimizer_success_pct?: number;
    sample_size?: number;
  };
  today_strategy?: string;
  cheap_threshold_pence?: number;
  peak_threshold_pence?: number;
  // Used by the Efficiency widget to soften the noisy KPIs when imports
  // are tiny (self-use day — VWAP / arbitrage% become uninformative
  // below ~3 kWh).
  today_import_kwh?: number;
  today_export_kwh?: number;
  // FIXED_TARIFF_* from env — empty/0 when the household hasn't configured
  // a "previous fixed contract" comparison. Used by TariffComparison to
  // replay BG Fixed v58 (etc.) against the real-usage block.
  fixed_tariff?: {
    label?: string | null;
    rate_pence?: number | null;
    standing_pence_per_day?: number | null;
  };
}

/* ----- /workbench (LP override editor) ----- */

export interface WorkbenchField {
  key: string;
  config_attr: string;
  type: string; // "float" | "int" | "str"
  min?: number | null;
  max?: number | null;
  enum?: string[] | null;
  description: string;
  group: string;
  promotable: boolean;
  current: number | string | boolean | null;
}

export interface WorkbenchSchema {
  groups: string[];
  fields: WorkbenchField[];
}

export interface WorkbenchSimSlot {
  t: string | null;
  price_p: number | null;
  import_kwh: number | null;
  export_kwh: number | null;
  battery_charge_kwh: number | null;
  battery_discharge_kwh: number | null;
  soc_kwh: number | null;
}

export interface WorkbenchSimulateResponse {
  ok: boolean;
  error?: string | null;
  plan_date?: string | null;
  objective_pence?: number | null;
  status?: string | null;
  slot_count?: number | null;
  actual_mean_agile_pence?: number | null;
  forecast_solar_kwh_horizon?: number | null;
  mu_load_kwh_per_slot?: number | null;
  applied_overrides: Record<string, unknown>;
  ignored_overrides: Record<string, unknown>;
  slots?: WorkbenchSimSlot[];
}

// ActionDiff shape from /workbench/promote/simulate. We render human_summary +
// the non-promotable list; the detailed diff items vary, so keep them loose.
export interface WorkbenchPromoteDiff {
  simulation_id: string;
  action?: string;
  human_summary?: string;
  non_promotable_overrides?: Record<string, unknown>;
}

export interface WorkbenchPromoteResult {
  ok: boolean;
  promoted: Array<{ key: string; ok: boolean; value?: unknown; error?: string }>;
  profile_name?: string | null;
}
