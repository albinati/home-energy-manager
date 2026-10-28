import { postJson, getJson, hemFetch } from "./api";
import type { RecommendationsResponse } from "./recTypes";
import type {
  CosyScorecardResponse,
  LwtLearningResponse,
  PlanFrontsResponse,
  CockpitNow,
  SettingsList,
  SimulateBatchResponse,
  ApplyBatchResponse,
  ApiQuotaResponse,
  FairCompareResponse,
  PeriodInsightsResponse,
  ActionLogResponse,
  StatusAlertsResponse,
  LpScorecardResponse,
  ActionDiffResponse,
  ProposePlanResponse,
  SensorDevicesResponse,
  DeviceLogResponse,
  ThermalCalibration,
  IndoorRollupResponse,
} from "./types";

/* ----- Real-time / cockpit ----- */

export const getCockpitNow = () => getJson<CockpitNow>("/cockpit/now");
export const getDaikinQuota = () => getJson<ApiQuotaResponse>("/daikin/quota");
export const getFoxQuota = () => getJson<ApiQuotaResponse>("/foxess/quota");

// Plan per front (battery / tank / heating) + consumption bands + spend score
// + tariff compare for one local day (#822). Cache-only, viewer-safe.
export const getPlanFronts = (date?: string) =>
  getJson<PlanFrontsResponse>(`/plan/fronts${date ? `?date=${encodeURIComponent(date)}` : ""}`);

// Comfort feedback (#833): admin POST; the row echoes back with its context.
export type ComfortVerdict = "cold" | "ok" | "hot";
export type ComfortFeedbackRow = {
  id: number; verdict: ComfortVerdict; room: string | null; indoor_c: number | null;
  band: string | null; lwt_offset_c: number | null;
};

/* ----- Ops status (alert strip + self-check, PR 3) -----
   Both endpoints are server-side TTL-cached (60s / 300s) with sub-caches on
   anything that costs vendor quota — polling them from every open tab can
   never amplify into live Fox/Daikin calls. Viewer-readable. */

export const getStatusAlerts = () => getJson<StatusAlertsResponse>("/status/alerts");
export const getLpScorecard = (date: string) =>
  getJson<LpScorecardResponse>(`/lp/scorecard/${encodeURIComponent(date)}`);

/* ----- Daikin controls (writes — require DAIKIN_CONTROL_MODE=active) -----
   The UI shows its own confirm dialog, then sends skip_confirmation:true so
   the backend doesn't return a separate pending-action step. A 409
   PassiveModeLocked surfaces as a HemApiError the caller can toast. */


/* ----- Forecast vs actuals ----- */

// Indoor climate sensors (#540 W1) — viewer-readable, one row per device.
export const getSensorDevices = () => getJson<SensorDevicesResponse>("/sensors/devices");
// Raw per-device reading log (#540 W1c) — the lossless audit of everything a
// sensor sent, newest first. Powers the Journal's sensor view.
export const getSensorDeviceLog = (device?: string, hours = 24) => {
  const qs = new URLSearchParams({ hours: String(hours) });
  if (device) qs.set("device", device);
  return getJson<DeviceLogResponse>(`/sensors/device-log?${qs.toString()}`);
};
// W2 thermal model state + learning progress (viewer).
export const getThermalCalibration = () =>
  getJson<ThermalCalibration>("/sensors/thermal-calibration");
// WARM-tier long-term indoor rollup (viewer).
export const getIndoorRollup = (days = 30) =>
  getJson<IndoorRollupResponse>(`/sensors/indoor-rollup?days=${days}`);

/* ----- Landing / story ----- */

export const getActionLog = (opts?: { device?: string; days?: number; limit?: number }) => {
  const p = new URLSearchParams();
  if (opts?.device) p.set("device", opts.device);
  if (opts?.days != null) p.set("days", String(opts.days));
  if (opts?.limit != null) p.set("limit", String(opts.limit));
  const qs = p.toString();
  return getJson<ActionLogResponse>(`/action-log${qs ? `?${qs}` : ""}`);
};

// /energy/period — granular chart_data (day=1pt, week=7pts, month=≤31pts,
// year=≤12pts). Per-point shape: { date, import_kwh, export_kwh,
// solar_kwh, load_kwh, charge_kwh, discharge_kwh }.
export const getEnergyPeriod = (
  period: "day" | "week" | "month" | "year",
  opts: { date?: string; month?: string; year?: number } = {},
) => {
  const qs = new URLSearchParams({ period });
  if (opts.date)  qs.set("date",  opts.date);
  if (opts.month) qs.set("month", opts.month);
  if (opts.year != null) qs.set("year", String(opts.year));
  return getJson<PeriodInsightsResponse>(`/energy/period?${qs.toString()}`);
};

// GET /tariffs/compare — fair per-slot tariff comparison for the navigator
// period. Your measured usage replayed against every tariff's own rate card
// (per-tariff standing + export; negative-price imports credit the bill).
export const getFairCompare = (
  gran: "day" | "week" | "month" | "year",
  anchor: string,
  maxTariffs = 14,
) =>
  getJson<FairCompareResponse>(
    `/tariffs/fair-compare?period=${gran}&anchor=${encodeURIComponent(anchor)}&max_tariffs=${maxTariffs}`,
  );

/* ----- Operate card (PR 4) — simulate→confirm control surface -----
   Every write here is paired with a /simulate that returns an ActionDiff
   (simulation_id + human_summary); the real call carries X-Simulation-Id.
   REQUIRE_SIMULATION_ID is ON in prod, so skipping simulate gets a 409. */


const postWithSimId = <T,>(path: string, simulationId: string) =>
  postJson<T>(path, {}, { headers: { "X-Simulation-Id": simulationId } });

export const simulateProposeOptimization = () =>
  postJson<ActionDiffResponse>("/optimization/propose/simulate", {});
export const proposeOptimization = (simulationId: string) =>
  postWithSimId<ProposePlanResponse>("/optimization/propose", simulationId);



/* ----- Settings ----- */

export const getSettings = () => getJson<SettingsList>("/settings");

export const simulateBatch = (changes: Record<string, unknown>) =>
  postJson<SimulateBatchResponse>("/settings/batch/simulate", { changes });

// The apply body is {changes: {KEY: value}} — the same shape simulate takes.
// (A bare array used to be sent here, which 422'd on the backend's dict body;
// review HIGH on #555.)
export async function applyBatch(
  simulationId: string,
  changes: Record<string, unknown>,
): Promise<ApplyBatchResponse> {
  const headers = new Headers({ "Content-Type": "application/json", "X-Simulation-Id": simulationId });
  const r = await hemFetch("/settings/batch", {
    method: "POST",
    body: JSON.stringify({ changes }),
    headers,
  });
  return r.json() as Promise<ApplyBatchResponse>;
}

/* ----- Workbench (LP override editor) ----- */





// --- Residual load profile (#477) — the learned household demand the LP plans
//     against (day-of-week median + p75 spread, measured-split calibrated).
export interface ResidualProfileSlot { h: number; m: number; median: number; p75?: number; }
export interface PvClearnessDay {
  date: string;
  solar_kwh: number | null;
  envelope_kwh: number | null;
  clearness: number | null;   // solar / trailing-21d-max; null for today (partial)
  load_kwh: number | null;
  import_kwh: number | null;
  export_kwh: number | null;
  partial: boolean;
}
export interface PvClearnessResponse { window_days: number; days: PvClearnessDay[] }
export const getPvClearness = (days = 30) =>
  getJson<PvClearnessResponse>(`/pv/clearness?days=${days}`);

export interface ResidualProfile {
  by_dow: Record<string, ResidualProfileSlot[]>;
  hp_by_dow?: Record<string, ResidualProfileSlot[]>;
  hp_dhw_by_dow?: Record<string, ResidualProfileSlot[]>;
  hp_space_by_dow?: Record<string, ResidualProfileSlot[]>;
  all: ResidualProfileSlot[];
  window_days?: number | null;
  end_date?: string | null;
  flat: number;
  away_days: string[];
  day_counts: { weekday?: number; weekend?: number; away_excluded?: number; negative_excluded?: number; total?: number };
  calibrated_days: number;
  physics_only_days: number;
}
export const getResidualProfile = (opts: { windowDays?: number; endDate?: string } = {}) => {
  const q = new URLSearchParams();
  if (opts.windowDays != null) q.set("window_days", String(opts.windowDays));
  if (opts.endDate) q.set("end_date", opts.endDate);
  const qs = q.toString();
  return getJson<ResidualProfile>(`/load/residual-profile${qs ? `?${qs}` : ""}`);
};

export interface LoadErrorStats {
  n: number;
  mae_kwh: number;
  bias_kwh: number;
  mean_forecast_kwh: number;
  mean_actual_kwh: number;
}
export interface LoadErrorLog {
  window_days: number;
  n_slots_logged: number;
  overall: LoadErrorStats;
  per_hour_local: Record<string, LoadErrorStats>;
}

export const getLoadErrorLog = (
  arg: number | { startDate: string; endDate: string } = 30,
) => {
  if (typeof arg === "number") return getJson<LoadErrorLog>(`/load/error-log?window_days=${arg}`);
  const q = new URLSearchParams({ start_date: arg.startDate, end_date: arg.endDate });
  return getJson<LoadErrorLog>(`/load/error-log?${q.toString()}`);
};

export const getCosyScorecard = (days = 14) =>
  getJson<CosyScorecardResponse>(`/scorecard/cosy?days=${days}`);

export const getLwtLearning = (days = 14) =>
  getJson<LwtLearningResponse>(`/thermal/lwt-learning?days=${days}`);

/* ----- Weekly tuning review (#832) — suggestions only ----- */

export interface TuningSuggestion {
  id: number;
  built_at_utc: string;
  week_start: string;
  key: string;
  current_value: string;
  suggested_value: string;
  delta_pence_per_week: number | null;
  delta_comfort_hours: number | null;
  verdict: "recommended" | "trade-off" | "comfort-first" | string;
  payload: {
    body?: { value: unknown };
    n_days?: number;
    cost_gbp_current?: number;
    cost_gbp_suggested?: number;
    hours_below_current?: number;
    hours_below_suggested?: number;
    comfort_first?: boolean;
    status?: "complete" | "partial" | string;
    n_variants_evaluated?: number;
    context?: { w3_active?: boolean; control_mode?: string; tariff_banded?: boolean; ua_w_per_k?: number | null };
  };
}
export interface TuningSuggestionsResponse { weeks: number; suggestions: TuningSuggestion[] }

export const getTuningSuggestions = (weeks = 4) =>
  getJson<TuningSuggestionsResponse>(`/tuning/suggestions?weeks=${weeks}`);

// Insights (#879 PR 2): the "O que fazer" list + week + house numbers. `range` is the
// navigator's week (Mon..Sun clamped to today, YYYY-MM-DD local); `fresh` bypasses the
// server's 10-minute cache (after an Aplicar).
export const getRecommendations = (range?: { start: string; end: string }, fresh = false) => {
  const q: string[] = [];
  if (range) q.push(`start=${encodeURIComponent(range.start)}`, `end=${encodeURIComponent(range.end)}`);
  if (fresh) q.push("fresh=1");
  return getJson<RecommendationsResponse>(`/insights/recommendations${q.length ? `?${q.join("&")}` : ""}`);
};
