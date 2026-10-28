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
  modes?: { daikin_control_mode?: string; optimization_preset?: string };
  /** #873 vacation snapshot (return_at is a local ISO datetime with offset). */
  vacation?: CockpitVacation | null;
  /** #872 Onecta holiday flags (null = unknown). */
  daikin?: { holiday?: { climate: boolean | null; tank: boolean | null; window?: unknown; cache_age_seconds?: number | null } | null };
  /** #879 vendor quota (same shape as /status/alerts quota). */
  quota?: { fox: CockpitQuotaEntry | null; daikin: CockpitQuotaEntry | null } | null;
  plan?: { pushed_at_utc: string | null };
  planner_tz?: string;
}
export interface CockpitVacation {
  active: boolean;
  return_at: string | null;
  recovery_at: string | null;
  daikin_holiday_until_sent: string | null;
  holiday_enabled: boolean;
}
export interface CockpitQuotaEntry { used: number | null; budget: number | null; blocked: boolean }

/* ----- /attribution/day ----- */

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

/* ----- /metrics (~1s, summary KPIs) ----- */

// Today's deterministic DHW tank plan — GET /api/v1/daikin/dhw-schedule.


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
    c_basis_ua_w_per_k?: number | null;  // #841: UA the stored C was derived from
    c_recomputed?: boolean;              // #841: C recomputed as tau x effective UA
    ua_learned_w_per_k?: number | null;  // #859: stored HDD-fit UA (in bounds), shown only unless auto-apply
    ua_pinned_w_per_k?: number;
    ua_effective_source?: "pin" | "learned";
    ua_learned_auto_apply?: boolean;
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

/* ----- GET /api/v1/plan/fronts (#822) — plan per front (battery / tank / heating),
   consumption probability bands, spend score, tariff compare. Every section is
   independently guarded server-side: a failing source yields `error` in that
   section and nulls elsewhere. ----- */
export type PlanWindowStatus = "done" | "ongoing" | "upcoming";

export interface PlanFrontsTariffWindow {
  key: string;
  label: string;
  start_utc: string;
  end_utc: string;
  start_local: string;
  end_local: string;
  price_p: number | null;
  status: PlanWindowStatus;
}
export interface PlanFrontsTariff {
  display_name: string | null;
  structure: "banded" | "dynamic" | "unknown";
  windows: PlanFrontsTariffWindow[];
  error?: string;
}

export type BatteryWindowKind = "grid_charge" | "pv_charge" | "hold" | "self_use" | "export" | "idle";
export interface BatteryPlanWindow {
  kind: BatteryWindowKind;
  start_utc: string;
  end_utc: string;
  start_local: string;
  end_local: string;
  grid_kwh: number | null;
  charge_kwh: number | null;
  discharge_kwh: number | null;
  soc_start_pct: number | null;
  soc_end_pct: number | null;
  fox_mode: "ForceCharge" | "Backup" | "SelfUse" | "ForceDischarge" | null;
}
export interface BatteryBandRow {
  key: string;
  label: string;
  start_local: string;
  end_local: string;
  planned_import_kwh: number | null;
  realised_import_kwh: number | null;
  soc_entry_pct: number | null;
  floored: boolean;
}
export interface FoxGroupRow {
  mode: string | null;
  start_local: string | null;
  end_local: string | null;
  min_soc: number | null;
  fd_soc: number | null;
  fd_pwr: number | null;
  max_soc: number | null;
}
export interface PlanFrontsBattery {
  capacity_kwh: number | null;
  reserve_pct: number | null;
  soc_now_pct: number | null;
  soc_now_kwh: number | null;
  plan_run_id: number | null;
  plan_run_at: string | null;
  windows: BatteryPlanWindow[];
  by_band: BatteryBandRow[];
  fox_groups: FoxGroupRow[];
  peak_import_planned_kwh: number | null;
  peak_import_realised_kwh: number | null;
  error?: string;
}

export interface TankModel {
  source: "databook" | "measured" | "measured_indoor" | null;
  ua_w_per_k: number | null;
  ambient_c: number | null;
  tau_hours: number | null;
  coast_measured_c_per_h: number | null;
  coast_model_c_per_h: number | null;
  coast_ratio: number | null;
}
export interface TankDecision {
  arm: "hold" | "boost" | "static" | null;
  warmup_hour: number | null;
  setback_hour: number | null;
  warmup_target_c: number | null;
  peak_entry_hour: number | null;
  cost_hold_p: number | null;
  cost_boost_p: number | null;
}
export interface TankPlanWindow {
  kind: "warmup" | "setback" | "boost" | "legionella";
  start_utc: string;
  end_utc: string;
  start_local: string;
  end_local: string;
  tank_target_c: number | null;
}
export interface TankShowerWindow {
  start_local: string;
  end_local: string;
  floor_c: number;
  label: string | null;
  predicted_tank_c: number | null;
}
export interface TankNextAction {
  kind: string;
  start_local: string;
  tank_target_c: number | null;
}
export interface PlanFrontsTank {
  tank_now_c: number | null;
  target_now_c: number | null;
  power_on: boolean | null;
  telemetry_at_utc: string | null;
  model: TankModel | null;
  decision: TankDecision | null;
  windows: TankPlanWindow[];
  showers: TankShowerWindow[];
  next_action: TankNextAction | null;
  error?: string;
}

export interface HeatingGate {
  preheat_enabled: boolean | null;
  demand_present: boolean | null;
  measured_window_kwh: number | null;
  threshold_kwh: number | null;
  current_outdoor_c: number | null;
  outdoor_cutoff_c: number | null;
  positive_offset_suppressed_by_outdoor: boolean | null;
  preheat_suppressed: boolean | null;
  lp_available: boolean | null;
}
export interface HeatingPlanWindow {
  kind: "boost" | "setback" | "restore";
  offset_c: number | null;
  start_utc: string;
  end_utc: string;
  start_local: string;
  end_local: string;
  source: "lp" | "tier" | null;
}
export interface HeatingPredictedIndoor {
  min_c: number | null;
  max_c: number | null;
  at_07_c: number | null;
  at_16_c: number | null;
  at_19_c: number | null;
  at_22_c: number | null;
}
export interface HeatingBandRow {
  key: string;
  label: string;
  start_local: string;
  end_local: string;
  indoor_min_c: number | null;
  indoor_max_c: number | null;
  offset_mode: "boost" | "setback" | "neutral" | null;
}
export interface PlanFrontsHeating {
  indoor_now_c: number | null;
  indoor_rooms_c: Record<string, number> | null;
  indoor_aggregate: string | null;
  outdoor_now_c: number | null;
  setpoint_c: number | null;
  night_floor_c: number | null;
  peak_coast_delta_c: number | null;
  night_start_hour?: number;
  night_end_hour?: number;
  ceiling_c?: number | null;
  lwt_source: "tier" | "lp" | null;
  coast_mode?: string | null;
  gate: HeatingGate | null;
  windows: HeatingPlanWindow[];
  predicted_indoor: HeatingPredictedIndoor | null;
  by_band: HeatingBandRow[];
  error?: string;
}

export interface ConsumptionExpected {
  p50: number | null;
  p75: number | null;
  p90: number | null;
  max?: number | null;
  n_days: number;
}
export interface ConsumptionBand {
  key: string;
  label: string;
  start_utc: string;
  end_utc: string;
  start_local: string;
  end_local: string;
  hours: number;
  price_p: number;
  status: PlanWindowStatus;
  progress: number;
  expected_kwh: ConsumptionExpected;
  committed_kwh: number | null;
  realised_kwh: number | null;
  forecast_error_kwh: {
    mean: number | null;
    p90: number | null;
    under_forecast_days: number;
    n_days: number;
  };
}
export interface PlanFrontsConsumption {
  date: string;
  now_utc: string;
  tariff_display_name: string | null;
  tariff_structure: string;
  day_type: string | null;
  history_days: number;
  bands: ConsumptionBand[];
  day: {
    expected_kwh: ConsumptionExpected;
    committed_kwh: number | null;
    realised_kwh: number | null;
  };
  error?: string;
}

export type SpendScore = "ideal" | "below" | "above";
export interface SpendPeriodRollup {
  n_days: number;
  net_cost_gbp: number | null;
  per_day_gbp: number | null;
  import_kwh: number | null;
  avg_import_p: number | null;
}
export interface PlanFrontsSpend {
  realised_import_kwh: number | null;
  realised_import_cost_gbp: number | null;
  realised_avg_import_p: number | null;
  forecast_import_kwh: number | null;
  forecast_avg_import_p: number | null;
  peak_import_kwh: number | null;
  ideal_avg_import_p: number | null;
  score: SpendScore | null;
  score_thresholds: { ideal_max_p: number | null; above_min_p: number | null } | null;
  score_basis: "realised" | "forecast" | null;
  pv_kwh?: number | null;
  /** Net realised today: import + standing - export (same basis as the bill). */
  realised_net_cost_gbp?: number | null;
  period: { week: SpendPeriodRollup | null; month: SpendPeriodRollup | null } | null;
  error?: string;
}

export interface CompareRow {
  product_code: string;
  display_name: string;
  net_gbp: number | null;
  approximate: boolean;
  is_current: boolean;
  delta_vs_current_gbp: number | null;
}
export interface PlanFrontsCompare {
  period: string;
  period_start: string;
  period_end: string;
  n_days: number;
  current: { product_code: string; display_name: string; net_gbp: number | null } | null;
  rows: CompareRow[];
  framing: string | null;
  error?: string;
}

/** #879 — Octopus bill cycle (cycle_start..cycle_end inclusive, ISO dates). */
export interface PlanFrontsBilling {
  cycle_start: string;
  cycle_end: string;
  days_elapsed: number;
  days_total: number;
  realised_gbp: number;
  /** null until one full day of the cycle is complete. */
  forecast_cycle_gbp: number | null;
  /** ISO date when the SMART_TARIFF_START_DATE clamp shortens the cycle. */
  since: string | null;
  current_label: string | null;
  compare: Array<{ tariff: string; label: string; short_label: string; delta_gbp: number; approximate: boolean; tie: boolean }>;
  winner: { tariff: string; label: string; is_current: boolean } | null;
  error?: string;
}

export interface PlanFrontsResponse {
  date: string;
  now_utc: string;
  tariff: PlanFrontsTariff | null;
  battery: PlanFrontsBattery | null;
  tank: PlanFrontsTank | null;
  heating: PlanFrontsHeating | null;
  consumption: PlanFrontsConsumption | null;
  spend: PlanFrontsSpend | null;
  compare: PlanFrontsCompare | null;
  billing?: PlanFrontsBilling | null;
}

// ---- Cosy daily scorecard (#831) — GET /api/v1/scorecard/cosy ----
export interface CosyScoreBand {
  key: string;
  label: string;
  is_peak: boolean;
  start_local: string;
  end_local: string;
  price_p: number | null;
  import_kwh: number | null;
  import_cost_gbp: number | null;
  load_error_kwh: number | null;
  under_forecast: boolean | null;
}

export interface CosyScoreRow {
  date: string;
  score: "ideal" | "below" | "above" | null;
  peak_import_kwh: number | null;
  import_kwh: number | null;
  import_cost_gbp: number | null;
  avg_import_p: number | null;
  ideal_avg_import_p: number | null;
  net_cost_gbp: number | null;
  built_at_utc: string;
  scored_at_local?: string | null;
  scheduled_at_local?: string | null;
  bands?: CosyScoreBand[] | null;
  peak_under_forecast?: boolean | null;
  battery?: { cycles?: number | null; pess_floor?: { floor_binding_slots?: number | null } } | null;
  comfort?: {
    hours_below_night_floor?: number | null;
    hours_below_peak_floor?: number | null;
    aggregate?: string;
  } | null;
  tank?: { any_below_floor?: boolean | null } | null;
  lwt?: { write_verify?: { success: number; unverified: number; mismatch: number } } | null;
  ops?: { daikin_calls?: number | null; fox_failures?: number | null } | null;
}

export interface CosyScorecardResponse {
  days: number;
  rows: CosyScoreRow[];
}

/* ----- LWT coast learning (#838) ----- */
export interface LwtJointFitSolve {
  ua_w_per_k: number | null;
  c_kwh_per_k: number | null;
  tau_h: number | null;
  c_se?: number | null;
  gain_kw?: number | null;
  resid_rms_c?: number | null;
  r2?: number | null;
  n_heat?: number;
  n_coast?: number;
}

export type LwtJointFitReason =
  | "too_few_heat_episodes"
  | "too_few_coast_blocks"
  | "nonphysical_fit"
  | "singular"
  | "no_measured_input";

/** #854 — why the solar gain could not be fitted. */
export type LwtSolarReason =
  | "no_daytime_coast_blocks"
  | "too_few_daytime_coast_blocks"
  | "nonphysical_fit"
  | "singular";

export interface LwtPredResidGroup {
  n: number;
  mean_c: number | null;
}

/** #854 — measured residual (real minus predicted indoor): positive = plan too cold = missing gains. */
export interface LwtPredResid {
  sign: string;
  day: LwtPredResidGroup;
  night: LwtPredResidGroup;
  by_pv_tercile: { tercile: "low" | "mid" | "high"; n: number; pv_mean_kw: number; mean_c: number | null }[];
}

/** Episode estimator (#843): one sample per Onecta-metered heating episode / night coast block. */
export interface LwtJointFit extends LwtJointFitSolve {
  identifiable: boolean;
  reason: LwtJointFitReason | null;
  ua_se: number | null;
  n_heat_episodes: number;
  n_coast_blocks: number;
  /** #854 solar / internal gains (thermal kW), analytics only. */
  solar_identifiable?: boolean;
  solar_reason?: LwtSolarReason | null;
  n_day_coast_blocks?: number;
  solar_gain_kw_per_pv_kw?: number | null;
  solar_gain_se?: number | null;
  internal_gain_kw?: number | null;
  internal_gain_se?: number | null;
  tau_prior_h: number | null;
  coast_tau_h: number | null;
  tau_fixed: LwtJointFitSolve | null;
  consistency_flag: "lag_or_gain_contamination_suspected" | null;
  slot_fit: LwtJointFitSolve | null;
  cop_sensitivity: Record<string, Pick<LwtJointFitSolve, "ua_w_per_k" | "c_kwh_per_k" | "tau_h"> | null> | null;
}

export interface LwtBandRise {
  start_utc: string;
  end_utc: string;
  n_slots: number;
  mean_offset_c: number;
  measured_rise_c: number;
  predicted_rise_c: number | null;
  model_error_c: number | null;
  plan_token: string | null;
  n_plans: number;
  mixed_plans: boolean;
}

export interface LwtLearningDaily {
  date: string;
  /** #843: true — the coast-only UA estimate is C(=tau x UA_pin) x decay rate. */
  ua_est_circular?: boolean;
  ua_from_tau_scaled_w_per_k?: number | null;
  ua_from_tau_scaled_night_w_per_k?: number | null;
  joint_fit?: LwtJointFit | null;
  joint_window_days?: number;
  night_rise_per_band?: LwtBandRise[];
  pred_resid?: LwtPredResid;
  n_coast_slots: number | null;
  n_heat_slots: number | null;
  ua_est_w_per_k: number | null;
  ua_est_night_w_per_k?: number | null;
  k_est_kw_per_c: number | null;
  pred_err_mean_c: number | null;
  pred_err_p90_c: number | null;
  ua_pinned_w_per_k?: number | null;
  k_pinned_kw_per_c?: number | null;
}

export interface LwtLearningSlot {
  slot_time_utc: string;
  coast_mode: string | null;
  offset_lp_raw: number | null;
  offset_written: number | null;
  indoor_pred_c: number | null;
  floor_c: number | null;
  indoor_real_c: number | null;
  indoor_min_c: number | null;
  device_offset: number | null;
  heating_kwh: number | null;
  price_band: string | null;
}

export interface LwtLearningResponse {
  days: number;
  coast_mode: string;
  timezone?: string;
  ua_pinned_w_per_k: number;
  /** #854: gains the LP currently assumes (0 = off). */
  internal_gain_pinned_kw?: number;
  solar_gain_pinned_kw_per_pv_kw?: number;
  daily: LwtLearningDaily[];
  yesterday: { date: string; slots: LwtLearningSlot[] };
}
