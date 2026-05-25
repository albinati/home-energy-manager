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

export interface AgileSlot {
  slot_time_utc: string;
  value_inc_vat: number;
  valid_from?: string;
  valid_to?: string;
}

export interface AgileDayResponse {
  date: string;
  import: AgileSlot[];
  export: AgileSlot[];
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
