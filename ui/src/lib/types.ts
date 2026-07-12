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

// POST /settings/batch/simulate returns an ActionDiff (src/api/simulation.py)
// whose sub_actions carry one per-key diff each: before/after are single-entry
// objects keyed by the setting ({KEY: current} / {KEY: proposed}).
// (The previous {diffs, warnings} shape here never matched the backend — the
// Settings simulate modal crashed on it; review HIGH on #555.)
export interface BatchSubAction {
  key: string;
  action: string;                    // "setting.<KEY>"
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  safety_flags: string[];
  human_summary: string;
}

export interface SimulateBatchResponse {
  action: string;                    // "settings.batch"
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  safety_flags: string[];
  human_summary: string;
  simulation_id: string;
  expires_at_epoch: number;
  sub_actions: BatchSubAction[];
}

// POST /settings/batch → {ok, results}; a mid-batch failure raises 409
// BatchPartialFailure instead (after best-effort rollback).
export interface ApplyBatchResponse {
  ok: boolean;
  results: Array<{ key: string; ok: boolean; value?: unknown; error?: string }>;
}

/* ----- /cockpit/now ----- */

// Per-room indoor sensor reading, folded into /cockpit/now (#540 W1).
export interface IndoorRoom {
  room: string;
  temp_c: number | null;
  humidity_pct: number | null;
  stale: boolean;
  age_min: number | null;
}

// Compact house indoor-climate snapshot carried in the live cockpit state.
export interface IndoorSummary {
  mean_c: number | null;        // mean over FRESH rooms (null when all stale)
  humidity_pct: number | null;  // mean humidity over fresh rooms
  n_rooms: number;
  n_fresh: number;
  stale: boolean;               // true when nothing is within the fresh window
  newest_captured_at: string | null;
  newest_received_at: string | null;
  rooms: IndoorRoom[];
}

export interface CockpitState {
  soc_pct: number;
  soc_kwh: number;
  solar_kw: number;
  load_kw: number;
  grid_kw: number;       // positive = importing; negative = exporting
  battery_kw: number;    // positive = charging
  tank_c: number | null;
  indoor_c: number | null;   // house mean from room sensors (null until one reports)
  lwt_c: number | null;
  daikin_mode: string | null;
  indoor?: IndoorSummary | null;   // rich per-room snapshot (null when no sensor)
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
  decision_reason?: string | null;   // dispatch_decisions.reason for this slot
  committed?: boolean | null;        // did the decision make it onto Fox V3
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
  base_load_kwh?: number | null;      // committed residual forecast (profile fallback where no solve covered the slot)
  load_forecast_kwh?: number | null;  // committed TOTAL household load (base + dhw + space), frozen at solve time
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

/* ----- /export/opportunity — money lost on flat SEG vs Outgoing Agile ----- */

export interface ExportOpportunityResponse {
  export_mode: string;             // "seg_flat" | "outgoing_agile"
  seg_rate_p: number;
  daily: Array<{ day: string; export_kwh: number; seg_gbp: number; agile_gbp: number; opportunity_gbp: number }>;
  n_days: number;
  export_kwh: number;
  seg_gbp: number;
  agile_gbp: number;
  opportunity_gbp: number;         // total Agile − SEG over the window (>0 = lost on SEG)
  annualized_gbp: number;
  avg_seg_p: number;
  avg_agile_p: number;
  today: { export_kwh: number; opportunity_gbp: number };
}

/* ----- /grid/today — per-slot planned-vs-realised grid import/export ----- */

export interface GridTodaySlot {
  slot_utc: string;
  import_planned_kwh: number | null;   // committed LP plan (stitched across solves)
  export_planned_kwh: number | null;
  import_actual_kwh: number | null;    // realised roll-up; null for future/no-telemetry
  export_actual_kwh: number | null;
  discharge_actual_kwh?: number | null; // battery discharge (covers load) — for the by-source view
  import_price_p?: number | null;
  kind?: string | null;
}

export interface GridTodayResponse {
  date: string;
  now_utc: string;
  slots: GridTodaySlot[];
  totals: {
    import_planned_kwh: number;
    export_planned_kwh: number;
    import_actual_kwh: number;
    export_actual_kwh: number;
  };
  plan_run_id?: number | null;
}

/* ----- /forecast/daily ----- */

export interface ForecastDailyDay {
  date: string;                       // local YYYY-MM-DD
  load_forecast_kwh: number | null;   // committed total-load forecast sum (load_error_log)
  load_actual_kwh: number | null;
  load_n_slots: number;
  pv_forecast_kwh: number | null;     // committed PV forecast sum (pv_error_log)
  pv_actual_kwh: number | null;
  pv_n_slots: number;
}

export interface ForecastDailyResponse {
  start_date: string;
  end_date: string;
  days: ForecastDailyDay[];
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
  cloud_cover_pct?: number | null;
  irradiance_wm2?: number | null;
  precipitation_mm?: number | null;
  weather_code?: number | null;   // WMO code (0 clear … 61-67 rain … 95-99 storm)
}

export interface WeatherResponse {
  forecast: WeatherSlot[];
  daikin?: {
    // No room_temp — the Altherma has no room stat. Indoor comes from the
    // house sensors via /cockpit/now (state.indoor), not the weather panel.
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
  daikin_tank_c?: number | null;
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
  export_mode?: string;             // "seg_flat" | "outgoing_agile"
  export_seg_rate_p?: number | null; // the flat rate actually earned (seg_flat)
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
  // Authoritative slot-level PnL vs the configured fixed tariff (British Gas),
  // import basis. Use THIS for "saved vs fixed" — the coarse delta_vs_fixed_*
  // above can flip sign on Agile months. null for pre-Agile months.
  delta_vs_fixed_real_pounds?: number | null;
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

// GET /energy/lifetime — pre-summed lifetime-on-Agile totals for the cockpit
// footer strip (replaces the six-call /energy/monthly fan-out). saved >= 0 =
// Agile beat the configured fixed tariff over the active months.
export interface EnergyLifetimeResponse {
  months: number;
  solar_kwh: number;
  export_kwh: number;
  saved_vs_fixed_pounds: number;
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

// GET /daikin/heating-plan — deterministic per-slot heating timeline across
// yesterday/today/tomorrow (#481 follow-up): outdoor temp + price tier + LWT
// offset + heating-on + tank target/kind, recomputed (no action_schedule).
export interface HeatingPlanSlot {
  slot_utc: string;
  outdoor_c?: number | null;
  price_p?: number | null;
  tier?: "negative" | "cheap" | "standard" | "peak" | null;
  lwt_offset?: number | null;     // integer °C, e.g. +3 / -2; null = no offset
  lwt_base_c?: number | null;     // weather-curve LWT at this outdoor temp (offset 0)
  lwt_setpoint_c?: number | null; // actual radiator target = base + offset
  heating_on?: boolean;
  tank_temp_c?: number | null;
  tank_kind?: "warmup" | "setback" | "boost" | null;
  indoor_planned_c?: number | null;  // W3 committed indoor plan; null when W3 off
}
export interface HeatingPlanDay {
  date: string;
  label: string;                  // "Yesterday" | "Today" | "Tomorrow"
  start_utc: string;
}
export interface HeatingPlanResponse {
  enabled: boolean;
  now_utc: string;
  high_temp_c: number;            // heating cutoff (DAIKIN_WEATHER_CURVE_HIGH_C)
  days: HeatingPlanDay[];
  slots: HeatingPlanSlot[];
}

// GET /energy/today-cumulative — today's grid traffic so far (to now). Real-
// money import cost goes NEGATIVE (a credit) on negative-price slots.
export interface TodayCumulativeResponse {
  date: string;
  consumption_kwh?: number;      // total household load so far today (hero headline)
  import_kwh: number;
  export_kwh: number;
  import_cost_gbp: number;       // <0 = we were paid to import (credit)
  export_revenue_gbp: number;
  // Real-money figures for the hero money block (Phase 3a).
  realised_net_cost_gbp?: number;            // the day's net bill so far; <0 = a credit/paid day
  standing_charge_gbp?: number;              // fixed daily standing charge baked into the net
  earnings_today_gbp?: number;               // money IN today: negative-import credit + export
  negative_import_credit_gbp?: number;       // the negative-price import credit part
  // The CONFIGURED fixed tariff (British Gas) — the correct comparison.
  fixed_tariff_label?: string | null;
  delta_vs_fixed_tariff_real_gbp?: number | null;  // £ cheaper than British Gas (>0 = Agile won)
  fixed_tariff_shadow_real_gbp?: number | null;    // what British Gas would have cost
  // "Meta a bater": avg import p/kWh Agile needs to match British Gas (standing
  // gap spread over the day's forecast grid import), vs the realised avg so far.
  breakeven_avg_import_p?: number | null;
  realised_avg_import_p?: number | null;
  forecast_import_kwh?: number;
}

// GET /appliances/suggestions — cheapest upcoming run window per idle appliance.
export interface ApplianceSuggestion {
  appliance_id: number;
  appliance_name: string;
  recommended_start_utc: string;
  recommended_end_utc: string;
  deadline_local: string;
  duration_minutes: number;
  avg_price_pence: number;
  est_kwh: number;
  est_cost_pence: number;        // signed: <0 = paid to run
  is_negative: boolean;
  meets_threshold?: boolean;     // true = genuinely cheap; false = "next/cheapest available"
}
export interface ApplianceSuggestionsResponse {
  suggestions: ApplianceSuggestion[];
  count: number;
}

// GET /appliances/jobs — a scheduled/running/completed appliance cycle.
export interface ApplianceJob {
  id: number;
  appliance_id: number;
  status: string;                // scheduled | running | completed | cancelled | …
  planned_start_utc: string | null;
  planned_end_utc: string | null;
  avg_price_pence: number | null;
  duration_minutes: number | null;
  deadline_utc: string | null;
}
export interface ApplianceJobsResponse {
  jobs: ApplianceJob[];
  count: number;
}

// GET /appliances — registered appliances.
export interface Appliance {
  id: number;
  name: string;
  device_type: string;
  enabled: boolean;
  effective_typical_kw?: number;
  deadline_local_time?: string;
}
export interface AppliancesResponse {
  appliances: Appliance[];
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

/* ----- /status/alerts + /status/feedback — ops health (PR 3, #553) ----- */

export interface StatusMeterBlock {
  last_day: string | null;
  age_days: number | null;
  stale: boolean;
}

export interface StatusLpBlock {
  failures_24h: number;
  last_failure: {
    run_at_utc?: string | null;
    error_class?: string | null;
    plan_date?: string | null;
  } | null;
}

export interface StatusForecastBlock {
  model_name: string | null;
  source: string | null;
  fetched_at_utc: string | null;
  age_s: number | null;
  sidecar_ok: boolean | null;
  degraded: boolean;
}

export interface StatusFoxDriftBlock {
  checked_at_utc: string;
  in_sync: boolean | null;   // null = live read failed (unknown, NOT drift)
  diff_count: number | null;
  error: string | null;
}

export interface StatusQuotaEntry {
  used: number | null;
  budget: number | null;
  blocked: boolean;
}

// Is the plan actually reaching the hardware? (the ~41h Fox-upload wedge of
// 2026-06-14 ran silently because nothing watched actuation freshness).
export interface StatusActuationBlock {
  fox: { last_upload_at: string | null; age_hours: number | null; stale: boolean } | null;
  daikin_tank: {
    last_at: string | null; age_hours: number | null;
    failed_24h: number; stale: boolean; failing: boolean;
  } | null;
  daikin_lwt: { failed_24h: number; failing: boolean } | null;
}

// Latest plan-vs-dispatch coherence audit — a planned battery hold that ended
// up SelfUse/absent is the 2026-07-10 incident signature. severe_count 0 =
// quiet (no recent audit, or the plan executed as committed).
export interface StatusCoherenceBlock {
  result: string | null;
  severe_count: number;
  severe: Array<Record<string, unknown>>;   // preview of the diverged slots
  matched?: number | null;
  mismatched?: number | null;
  total_slots?: number | null;
  ts: string | null;
}

export interface StatusAlertsResponse {
  now_utc: string;
  meter: StatusMeterBlock;
  lp: StatusLpBlock;
  forecast: StatusForecastBlock;
  fox_drift: StatusFoxDriftBlock;
  quota: { fox: StatusQuotaEntry | null; daikin: StatusQuotaEntry | null };
  actuation?: StatusActuationBlock;
  coherence?: StatusCoherenceBlock;
}

export interface DhwBudgetState {
  mode: string;
  nominal_kwh: number | null;
  autoscale_factor: number | null;
  autoscale_enabled: boolean;
  effective_budget_kwh: number | null;
  measured_today_kwh: number | null;
  measured_7d_avg_kwh: number | null;
}

export interface LwtGateState {
  preheat_enabled: boolean;
  gate_enabled: boolean;
  demand_present: boolean;
  measured_window_kwh: number | null;
  threshold_kwh: number;
  lookback_hours: number;
  preheat_suppressed: boolean;
  // Exogenous warm-day cutoff (#540) — optional so an older hem image (which
  // doesn't send them) degrades to the demand-gate-only label.
  outdoor_cutoff_c?: number;
  current_outdoor_c?: number | null;
  positive_offset_suppressed_by_outdoor?: boolean;
}

/** Observational price-aware DHW warmup would-pick (#683 shadow). The feature
 * is OFF by default; this row just records what the price-aware resolver WOULD
 * have picked vs the static hour, so the delta can be watched toward a winter
 * enable-decision. null until today's (or D+1's) Agile window is fully real. */
export interface DhwWarmupShadow {
  static_hour: number;
  would_pick_hour: number;
  delta_pence: number | null;
  enabled: boolean;
  resolved_at: string;
}

export interface StatusFeedbackResponse {
  now_utc: string;
  dhw: DhwBudgetState;
  dhw_warmup_shadow: DhwWarmupShadow | null;
  lwt_gate: LwtGateState;
  forecast: StatusForecastBlock;
}

/* ----- /recent-triggers (admin-only read) ----- */

export interface TriggerRow {
  id: number;
  timestamp: string;
  device: string | null;
  action: string | null;
  params: string | null;
  result: string | null;
  error_msg: string | null;
  trigger: string | null;
  slot_kind: string | null;
  agile_price_at_time: number | null;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  actor: string | null;
}

export interface RecentTriggersResponse {
  rows: TriggerRow[];
  count: number;
}

/* ----- /lp/scorecard/{date} ----- */

export interface LpScorecard {
  day: string;
  forecast_accuracy: {
    available?: boolean;
    n_hours?: number;
    pv_kwh_mae?: number;
    pv_kwh_bias?: number;       // positive = over-forecast
    outdoor_temp_c_mae?: number;
    outdoor_temp_c_bias?: number;
    load_kwh_mae?: number;
    load_kwh_bias?: number;
  } | null;
  dispatch_accuracy: {
    n_slots_with_plan?: number;
    n_slots_with_real?: number;
    import_planned_kwh?: number;
    import_real_kwh?: number;
    import_accuracy_pct?: number | null;
    export_planned_kwh?: number;
    export_real_kwh?: number;
    export_accuracy_pct?: number | null;
    charge_planned_kwh?: number;
    charge_real_kwh?: number;
    charge_accuracy_pct?: number | null;
  } | null;
  economic_value: {
    lp_realised_cost_p?: number;
    naive_self_use_shadow_p?: number;
    lp_avoided_cost_p?: number;    // positive = LP saved money vs naive
    comparison_basis?: string;
  } | null;
  grade: string | null;
}

export interface LpScorecardResponse {
  ok: boolean;
  scorecard: LpScorecard;
}

/* ----- Operate card (PR 4): scheduler status + ActionDiff simulate flow ----- */

export interface SchedulerStatus {
  enabled: boolean;
  paused: boolean;
  current_price_pence?: number | null;
  next_cheap_from?: string | null;
  next_cheap_to?: string | null;
  planned_lwt_adjustment?: number;
  tariff_code?: string | null;
}

// Shape of every POST /…/simulate response (src/api/simulation.py ActionDiff).
export interface ActionDiffResponse {
  action: string;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  affected_slots: string[];
  cost_delta_pence: number | null;
  soc_path_change: number[];
  safety_flags: string[];
  human_summary: string;
  simulation_id: string;
  expires_at_epoch: number;
  sub_actions: Array<Record<string, unknown>>;
}

export interface ProposePlanResponse {
  plan_id: string;
  proposed_at: string;
  expires_at?: string | null;
  status: string;
  summary?: string | null;
}

// Indoor climate sensors (#540 W1) — one entry per device from
// GET /api/v1/sensors/devices, with the latest reading's metrics attached.
export interface SensorDeviceLatest {
  temp_c: number | null;
  humidity_pct: number | null;
  pressure_hpa: number | null;
  captured_at: string | null;
  received_at: string | null;
}

export interface SensorDevice {
  device_key: string;
  device_id: string | null;
  mac: string | null;
  room: string | null;
  source: string | null;
  n_readings: number;
  first_seen: string | null;
  last_seen: string | null;
  latest: SensorDeviceLatest | null;
}

export interface SensorDevicesResponse {
  n_devices: number;
  devices: SensorDevice[];
}

// GET /sensors/device-log — the lossless per-device audit (#540 W1c): every
// reading a sensor POSTed, with the full original `payload` attached.
export interface DeviceLogRow {
  received_at: string;
  captured_at: string | null;
  device_key: string;
  device_id: string | null;
  mac: string | null;
  room: string | null;
  source: string | null;
  temp_c: number | null;
  humidity_pct: number | null;
  pressure_hpa: number | null;
  payload: Record<string, unknown> | null;
}
export interface DeviceLogResponse {
  device: string | null;
  hours: number;
  n_rows: number;
  rows: DeviceLogRow[];
}

// GET /sensors/indoor?hours=N — recent per-reading rows (all rooms), for the
// heating chart's realised indoor-temp line.
export interface IndoorReadingRow {
  captured_at: string;
  room: string;
  temp_c: number;
  source?: string | null;
  quality?: string | null;
}
export interface IndoorReadingsResponse {
  hours: number;
  n_readings: number;
  readings: IndoorReadingRow[];
  newest_at: string | null;
  stale_minutes: number;
  configured: boolean;
}

// GET /sensors/indoor-rollup — WARM-tier 15-min indoor rollup (long-term trend).
export interface IndoorRollupBucket {
  bucket_utc: string;
  room: string;
  mean_c: number;
  min_c: number;
  max_c: number;
  n: number;
}
export interface IndoorRollupResponse {
  days: number;
  room: string | null;
  n_buckets: number;
  buckets: IndoorRollupBucket[];
}

// GET /sensors/thermal-calibration — W2 thermal model state + learning progress.
export interface ThermalProgressPart {
  status: string | null;
  needed: number;
  reason: string | null;
  episodes?: number | null;   // τ side
  hdd_days?: number | null;   // UA side
}
export interface ThermalCalibration {
  calibration: Record<string, unknown> | null;
  effective: {
    tau_hours: number;
    ua_w_per_k: number;
    c_kwh_per_k: number;
    source: "env" | "learned";
  };
  progress: {
    last_run_utc: string | null;
    status: string | null;
    tau: ThermalProgressPart;
    ua: ThermalProgressPart;
  } | null;
  w3_enabled: boolean;   // W3: LP comfort-optimising vs weather curve
  learning_enabled: boolean;
  learned_values_enabled: boolean;
}
