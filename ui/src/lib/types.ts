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

export interface ExecutionSlot {
  slot_time_utc: string;
  soc_percent: number | null;
  consumption_kwh: number | null;
  fox_mode?: string | null;
  daikin_tank_temp?: number | null;
  daikin_room_temp?: number | null;
  pv_kwh?: number | null;
  import_kwh?: number | null;
  export_kwh?: number | null;
  outdoor_temp_c?: number | null;
}

export interface ExecutionTodayResponse {
  date: string;
  slots: ExecutionSlot[];
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

/* ----- /energy/report + /energy/monthly + /tariffs/dashboard ----- */

export interface EnergyReport {
  period?: string;
  pnl?: {
    realised_cost_gbp?: number;
    realised_net_cost_gbp?: number;
    realised_import_gbp?: number;
    export_revenue_gbp?: number;
    standing_charge_gbp?: number;
    svt_shadow_gbp?: number;
    fixed_shadow_gbp?: number;
    fixed_tariff_shadow_gbp?: number;
    delta_vs_svt_gbp?: number;
    delta_vs_fixed_gbp?: number;
    delta_vs_fixed_tariff_gbp?: number;
    delta_vs_svt_real_gbp?: number;
    delta_vs_fixed_real_gbp?: number;
    delta_vs_fixed_tariff_real_gbp?: number;
  };
  tariff_comparison?: {
    agile?: number;
    go?: number;
    fixed?: number;
  };
  heating?: {
    kwh_for_showers?: number;
    kwh_for_heating?: number;
  };
}

export interface MonthlyEnergy {
  month: string;
  cost_gbp: number;
  import_kwh: number;
  export_kwh: number;
  solar_kwh: number;
  peak_import_pct?: number;
  peak_ratio?: number;
  savings_vs_svt_gbp?: number;
  battery_cycles?: number;
}

export interface TariffComparisonRow {
  tariff_code: string;
  tariff_name?: string;
  total_cost_gbp: number;
  monthly_cost_gbp?: number;
  savings_vs_active_gbp?: number;
}

export interface TariffDashboardResponse {
  baseline_tariff?: string;
  tariff_comparison?: TariffComparisonRow[];
  daily_costs?: Array<{ date: string; cost_gbp: number; tariff_code: string }>;
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
  quota_used_24h?: number;
  quota_remaining_24h?: number;
  daily_budget?: number;
  blocked?: boolean;
  last_blocked_at?: number | null;
}

// /load/breakdown — components of consumption
export interface LoadBreakdownComponent {
  name: string;
  kwh: number;
  pct_of_total: number;
}
export interface LoadBreakdownResponse {
  components: LoadBreakdownComponent[];
  total_kwh: number;
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
}
