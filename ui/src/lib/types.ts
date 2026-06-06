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

/* ----- /pv/today ----- */

export interface PvTodaySlot {
  slot_utc: string;
  pv_forecast_kwh: number;            // live forecast (re-fetched per request)
  pv_planned_kwh?: number | null;     // committed plan (frozen since last LP solve)
  pv_actual_kwh: number | null;
  import_price_p?: number | null;
  base_load_kwh?: number | null;
  kind?: string | null;
}

export interface PvTodayAccuracy {
  slots_compared: number;
  forecast_kwh: number;
  actual_kwh: number;
  mae_kwh: number;
  bias_kwh: number;
}

export interface PvTodayResponse {
  date: string;
  now_utc: string;
  slots: PvTodaySlot[];
  accuracy: PvTodayAccuracy | null;
  forecast_kwh_day_total: number;
  plan_committed_at?: string | null;  // ISO ...Z of the committed LP solve
  plan_run_id?: number | null;
}

/* ----- /optimization/inputs ----- */

export interface OptimizationInputSlot {
  t_utc: string;
  price_import_p?: number | null;
  price_export_p?: number | null;
  temp_c?: number | null;
  solar_w_m2?: number | null;
  base_load_kwh?: number | null;
}

export interface OptimizationInputsResponse {
  slots: OptimizationInputSlot[];
  thresholds?: { cheap_p?: number; peak_p?: number } | null;
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
  appliance_kwh_est?: number | null;   // estimated appliance load (armed jobs)
  base_load_kwh_est?: number | null;    // residual − appliance = measured base load
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
  // Fixed-tariff counterfactual on the same metered kWh + day-window as the
  // realised cost. null unless FIXED_TARIFF_* is configured. Positive delta =
  // Agile cheaper than the fixed tariff over this period.
  fixed_shadow_pence?: number | null;
  fixed_shadow_pounds?: number | null;
  delta_vs_fixed_pence?: number | null;
  delta_vs_fixed_pounds?: number | null;
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
  lwt_offset?: number | null;
  weather_regulation?: boolean | null;
  control_mode?: string | null;
  state_summary?: string | null;
  // is_on === climate (space-heating) onOffMode; prefer climate_on / dhw_on
  // (unambiguous, what /daikin/status actually serves). tank_power is legacy
  // and NOT populated by the status route — use dhw_on for the tank.
  is_on?: boolean | null;
  climate_on?: boolean | null;
  dhw_on?: boolean | null;
  tank_power?: boolean | null;
}

// Today's deterministic DHW tank plan — GET /api/v1/daikin/dhw-schedule.
export interface DhwScheduleRow {
  action_type?: string | null;   // tank_warmup | tank_setback | tank_negative_boost
  start_utc?: string | null;
  end_utc?: string | null;
  tank_temp_c?: number | null;
}
export interface DhwScheduleResponse {
  mode: string;
  rows: DhwScheduleRow[];
}

// GET /daikin/lwt-schedule — committed LWT-offset pre-heat plan (#481).
// Per-slot integer offset: positive = boost (cheap), negative = setback (peak).
export interface LwtScheduleRow {
  action_type?: string | null;   // lwt_preheat
  start_utc?: string | null;
  end_utc?: string | null;
  lwt_offset?: number | null;     // integer °C, e.g. +3 / -2
  status?: string | null;
}
export interface LwtScheduleResponse {
  enabled: boolean;
  rows: LwtScheduleRow[];
}

// GET /energy/today-cumulative — today's grid traffic so far (to now). Real-
// money import cost goes NEGATIVE (a credit) on negative-price slots.
export interface TodayCumulativeResponse {
  date: string;
  import_kwh: number;
  export_kwh: number;
  import_cost_gbp: number;       // <0 = we were paid to import (credit)
  export_revenue_gbp: number;
}

// One executed action from action_log — GET /action-log.
export interface ActionLogEntry {
  id: number;
  timestamp: string;
  device: string;                // daikin | foxess | appliance
  action: string;                // tank_warmup | max_heat | charge | washer_start | …
  params: Record<string, unknown>;
  result: string;                // success | failed | skipped
  error_msg: string | null;
  trigger: string | null;        // lp_dispatch | negative_window | user_manual | …
  slot_kind: string | null;      // cheap | peak | negative | standard
  agile_price_at_time: number | null;
  actor?: string | null;
}
export interface ActionLogResponse {
  entries: ActionLogEntry[];
}

// Daikin operation modes accepted by POST /daikin/mode.
export type DaikinOperationMode = "heating" | "cooling" | "auto" | "fan_only" | "dry";

// Shape returned by the Daikin write routes (ActionResult). When a route
// requires confirmation and skip_confirmation is false it returns a different
// (pending) shape — the UI always sends skip_confirmation:true after its own
// confirm dialog, so it only ever sees this.
export interface ActionResult {
  success: boolean;
  message: string;
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
  // Daikin only — DAIKIN_CONTROL_MODE, surfaced so the heating lock/active
  // state shows even when device telemetry is cold (quota blocked).
  control_mode?: string | null;
  // Daikin only — manual force-refresh cooldown, so the UI button locks +
  // counts down in lock-step with the server-side per-actor throttle.
  force_refresh_min_interval_seconds?: number;
  force_refresh_available_in_seconds?: number;
}

/* ----- GET /tariffs/compare — fair per-slot tariff comparison ----- */

export interface FairTariffRow {
  product_code: string;
  display_name: string;
  pricing: "half_hourly" | "time_of_use" | "flat" | string;
  is_current: boolean;
  approximate: boolean;          // non-current half-hourly priced by proxy
  import_cost_pence: number;
  standing_pence: number;
  export_credit_pence: number;
  negative_credit_pence: number; // ≤0 — bill credit from negative-price imports
  net_pence: number;             // import + standing − export_credit
  import_kwh: number;
  export_kwh: number;
  n_days: number;
}

export interface FairCompareResponse {
  period_start: string;
  period_end: string;
  requested_start?: string | null;
  clamped: boolean;
  clamp_reason?: string | null;
  n_days: number;
  days_with_data: number;
  basis: { import_kwh: number; export_kwh: number };
  current_product_code: string;
  tariffs: FairTariffRow[];
  winner_product_code?: string | null;
  savings_vs_current_pounds: number;
  catalogue_unavailable: boolean;
  data_source: string;
  export?: FairCompareExport | null;
}

export interface FairCompareExport {
  export_kwh: number;
  mode: string;                  // seg_flat | outgoing_agile (the actual one)
  seg_rate_p: number;
  seg_revenue_pence: number;     // flat SEG revenue (actual)
  agile_revenue_pence: number;   // Outgoing Agile alternative on the same kWh
  agile_avg_p: number;
  uplift_if_switch_pence: number;
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
  // a "previous fixed contract" comparison. The fair compare engine replays it
  // (BG Fixed v58, etc.) against the measured-usage block as a candidate tariff.
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
