import { postJson, getJson, hemFetch } from "./api";
import type {
  CockpitNow,
  SchedulerTimeline,
  DispatchDecisionsResponse,
  WeatherResponse,
  ExecutionTodayResponse,
  AgileTodayResponse,
  AgileDaySlotsResponse,
  OctopusConsumptionResponse,
  PvCalibration,
  AttributionDay,
  EnergyReport,
  MonthlyEnergy,
  EnergyLifetimeResponse,
  SettingsList,
  SimulateBatchResponse,
  ApplyBatchResponse,
  MetricsResponse,
  DaikinDevice,
  ApiQuotaResponse,
  FairCompareResponse,
  PeriodInsightsResponse,
  DaikinConsumptionResponse,
  DhwScheduleResponse,
  HeatingPlanResponse,
  PvTodayResponse,
  GridTodayResponse,
  ExportOpportunityResponse,
  OptimizationInputsResponse,
  ActionResult,
  DaikinOperationMode,
  WorkbenchSchema,
  WorkbenchSimulateResponse,
  WorkbenchPromoteDiff,
  WorkbenchPromoteResult,
  TodayCumulativeResponse,
  ActionLogResponse,
  ApplianceSuggestionsResponse,
  ApplianceJobsResponse,
  AppliancesResponse,
  StatusAlertsResponse,
  StatusFeedbackResponse,
  RecentTriggersResponse,
  LpScorecardResponse,
  SchedulerStatus,
  ActionDiffResponse,
  ProposePlanResponse,
  ForecastDailyResponse,
  SensorDevicesResponse,
  IndoorReadingsResponse,
  ThermalCalibration,
} from "./types";

/* ----- Real-time / cockpit ----- */

export const getCockpitNow = () => getJson<CockpitNow>("/cockpit/now");
export const getSchedulerTimeline = () => getJson<SchedulerTimeline>("/scheduler/timeline");
export const getDecisionsLatest = () =>
  getJson<DispatchDecisionsResponse>("/optimization/decisions/latest");
export const getMetrics = () => getJson<MetricsResponse>("/metrics");
export const getPvToday = (date?: string) =>
  getJson<PvTodayResponse>(`/pv/today${date ? `?date=${encodeURIComponent(date)}` : ""}`);
// Per-slot planned-vs-realised GRID import/export for a UTC day (#3b). Mirrors
// /pv/today; powers the synced Grid timeline widget.
export const getGridToday = (date?: string) =>
  getJson<GridTodayResponse>(`/grid/today${date ? `?date=${encodeURIComponent(date)}` : ""}`);
// Running tally of export £ left on the table by being on flat SEG vs Agile.
export const getExportOpportunity = (days = 60) =>
  getJson<ExportOpportunityResponse>(`/export/opportunity?days=${days}`);
export const getOptimizationInputs = () =>
  getJson<OptimizationInputsResponse>("/optimization/inputs");
export const getDaikinStatus = () => getJson<DaikinDevice[]>("/daikin/status");
// Explicit, user-triggered LIVE read (one Daikin API call). Everything else
// reads the cache the LP/scheduler already refreshed (~30 min cadence).
export const forceRefreshDaikin = () => getJson<DaikinDevice[]>("/daikin/status?refresh=true");
export const getDaikinQuota = () => getJson<ApiQuotaResponse>("/daikin/quota");
export const getFoxQuota = () => getJson<ApiQuotaResponse>("/foxess/quota");
// Today's deterministic DHW tank plan (times + targets). Zero Daikin quota.
export const getDhwSchedule = () => getJson<DhwScheduleResponse>("/daikin/dhw-schedule");
// Per-slot heating-plan timeline (D-1/D/D+1): outdoor temp + LWT offset + tank
// + heating-on, deterministically recomputed. Zero Daikin quota.
export const getHeatingPlan = () => getJson<HeatingPlanResponse>("/daikin/heating-plan");

/* ----- Ops status (alert strip + self-check, PR 3) -----
   Both endpoints are server-side TTL-cached (60s / 300s) with sub-caches on
   anything that costs vendor quota — polling them from every open tab can
   never amplify into live Fox/Daikin calls. Viewer-readable. */

export const getStatusAlerts = () => getJson<StatusAlertsResponse>("/status/alerts");
export const getStatusFeedback = () => getJson<StatusFeedbackResponse>("/status/feedback");
// Admin-only read (middleware admin_read_prefixes) — callers must role-gate.
export const getRecentTriggers = (limit = 6) =>
  getJson<RecentTriggersResponse>(`/recent-triggers?limit=${limit}`);
export const getLpScorecard = (date: string) =>
  getJson<LpScorecardResponse>(`/lp/scorecard/${encodeURIComponent(date)}`);

/* ----- Daikin controls (writes — require DAIKIN_CONTROL_MODE=active) -----
   The UI shows its own confirm dialog, then sends skip_confirmation:true so
   the backend doesn't return a separate pending-action step. A 409
   PassiveModeLocked surfaces as a HemApiError the caller can toast. */
export const setTankTemperature = (temperature: number) =>
  postJson<ActionResult>("/daikin/tank-temperature", { temperature });
export const setTankPower = (on: boolean) =>
  postJson<ActionResult>("/daikin/tank-power", { on, skip_confirmation: true });
// Climate (space-heating) zone power — mirrors tank-power.
export const setClimatePower = (on: boolean) =>
  postJson<ActionResult>("/daikin/power", { on, skip_confirmation: true });
export const setLwtOffset = (offset: number) =>
  postJson<ActionResult>("/daikin/lwt-offset", { offset });
export const setDaikinMode = (mode: DaikinOperationMode) =>
  postJson<ActionResult>("/daikin/mode", { mode });

// /daikin/consumption — Onecta-measured Daikin energy split by heating vs DHW.
// SQLite read only — zero Daikin API quota. Granularities mirror /energy/period.
export const getDaikinConsumption = (
  period: "day" | "week" | "month" | "year",
  opts: { date?: string; month?: string; year?: number } = {},
) => {
  const qs = new URLSearchParams({ period });
  if (opts.date)  qs.set("date",  opts.date);
  if (opts.month) qs.set("month", opts.month);
  if (opts.year != null) qs.set("year", String(opts.year));
  return getJson<DaikinConsumptionResponse>(`/daikin/consumption?${qs.toString()}`);
};

/* ----- Forecast vs actuals ----- */

export const getWeather = () => getJson<WeatherResponse>("/weather");
// Indoor climate sensors (#540 W1) — viewer-readable, one row per device.
export const getSensorDevices = () => getJson<SensorDevicesResponse>("/sensors/devices");
// Indoor sensor history (all rooms) for the last N hours — the realised
// indoor-temp line on the heating chart.
export const getIndoorReadings = (hours = 24) =>
  getJson<IndoorReadingsResponse>(`/sensors/indoor?hours=${hours}`);
// W2 thermal model state + learning progress (viewer).
export const getThermalCalibration = () =>
  getJson<ThermalCalibration>("/sensors/thermal-calibration");
export const getExecutionToday = (date?: string) =>
  getJson<ExecutionTodayResponse>(`/execution/today${date ? `?date=${encodeURIComponent(date)}` : ""}`);
export const getAgileToday = () => getJson<AgileTodayResponse>("/agile/today");
export const getAgileDay = (date: string) =>
  getJson<AgileDaySlotsResponse>(`/agile/day?date=${encodeURIComponent(date)}`);
export const getOctopusConsumption = () =>
  getJson<OctopusConsumptionResponse>("/octopus/consumption");
export const getPvCalibration = () =>
  getJson<PvCalibration>("/patterns/pv-calibration");

/* ----- Landing / story ----- */

// /energy/report defaults to period=month on the backend. Pass period="day"
// to get a single-day rollup — same shape, just covering one day. Critical
// for "Today's bill" since we want today, not the whole month.
export const getEnergyReport = (date?: string, period: "day" | "month" = "day") =>
  getJson<EnergyReport>(
    date
      ? `/energy/report?date=${encodeURIComponent(date)}&period=${period}`
      : `/energy/report?period=${period}`,
  );
export const getEnergyMonthly = (month: string) =>
  getJson<MonthlyEnergy>(`/energy/monthly?month=${encodeURIComponent(month)}`);
// Pre-summed lifetime-on-Agile totals for the cockpit footer — one cached
// call replacing the old six-month /energy/monthly fan-out (each of which
// re-ran an uncached ~1-2.7s PnL replay; 2026-06-13 perf audit).
export const getEnergyLifetime = (months = 6) =>
  getJson<EnergyLifetimeResponse>(`/energy/lifetime?months=${months}`);
export const getEnergyTodayCumulative = () =>
  getJson<TodayCumulativeResponse>("/energy/today-cumulative");
export const getApplianceSuggestions = () =>
  getJson<ApplianceSuggestionsResponse>("/appliances/suggestions");
export const getApplianceJobs = (opts?: { status?: string; limit?: number }) => {
  const p = new URLSearchParams();
  if (opts?.status) p.set("status", opts.status);
  if (opts?.limit != null) p.set("limit", String(opts.limit));
  const qs = p.toString();
  return getJson<ApplianceJobsResponse>(`/appliances/jobs${qs ? `?${qs}` : ""}`);
};
export const getAppliances = () =>
  getJson<AppliancesResponse>("/appliances");
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
export const getAttributionDay = (date?: string) =>
  getJson<AttributionDay>(date ? `/attribution/day?date=${encodeURIComponent(date)}` : "/attribution/day");

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

export const getSchedulerStatus = () => getJson<SchedulerStatus>("/scheduler/status");

const postWithSimId = <T,>(path: string, simulationId: string) =>
  postJson<T>(path, {}, { headers: { "X-Simulation-Id": simulationId } });

export const simulateProposeOptimization = () =>
  postJson<ActionDiffResponse>("/optimization/propose/simulate", {});
export const proposeOptimization = (simulationId: string) =>
  postWithSimId<ProposePlanResponse>("/optimization/propose", simulationId);

export const simulateSchedulerPause = () =>
  postJson<ActionDiffResponse>("/scheduler/pause/simulate", {});
export const pauseScheduler = (simulationId: string) =>
  postWithSimId<{ status: string }>("/scheduler/pause", simulationId);
export const simulateSchedulerResume = () =>
  postJson<ActionDiffResponse>("/scheduler/resume/simulate", {});
export const resumeScheduler = (simulationId: string) =>
  postWithSimId<{ status: string }>("/scheduler/resume", simulationId);

// Cancelling a scheduled appliance run is already a two-step consent (the
// job only exists because the user armed the appliance physically) — no
// simulate pair exists for it.
export const cancelApplianceJob = (jobId: number) =>
  postJson<{ id: number; status: string }>(`/appliances/jobs/${jobId}/cancel`, {});

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

export const getWorkbenchSchema = () => getJson<WorkbenchSchema>("/workbench/schema");

export const simulateWorkbench = (overrides: Record<string, unknown>) =>
  postJson<WorkbenchSimulateResponse>("/workbench/simulate", { overrides });

export const promoteSimulateWorkbench = (overrides: Record<string, unknown>) =>
  postJson<WorkbenchPromoteDiff>("/workbench/promote/simulate", { overrides });

export async function promoteWorkbench(
  simulationId: string,
  overrides: Record<string, unknown>,
  profileName?: string,
): Promise<WorkbenchPromoteResult> {
  const headers = new Headers({ "Content-Type": "application/json", "X-Simulation-Id": simulationId });
  const r = await hemFetch("/workbench/promote", {
    method: "POST",
    body: JSON.stringify({ overrides, profile_name: profileName }),
    headers,
  });
  return r.json() as Promise<WorkbenchPromoteResult>;
}

// --- Residual load profile (#477) — the learned household demand the LP plans
//     against (day-of-week median + p75 spread, measured-split calibrated).
export interface ResidualProfileSlot { h: number; m: number; median: number; p75?: number; }
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
export const getForecastDaily = (startDate: string, endDate: string) => {
  const q = new URLSearchParams({ start_date: startDate, end_date: endDate });
  return getJson<ForecastDailyResponse>(`/forecast/daily?${q.toString()}`);
};

export const getLoadErrorLog = (
  arg: number | { startDate: string; endDate: string } = 30,
) => {
  if (typeof arg === "number") return getJson<LoadErrorLog>(`/load/error-log?window_days=${arg}`);
  const q = new URLSearchParams({ start_date: arg.startDate, end_date: arg.endDate });
  return getJson<LoadErrorLog>(`/load/error-log?${q.toString()}`);
};
