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
  PvTodayResponse,
  OptimizationInputsResponse,
  ActionResult,
  DaikinOperationMode,
  WorkbenchSchema,
  WorkbenchSimulateResponse,
  WorkbenchPromoteDiff,
  WorkbenchPromoteResult,
  TodayCumulativeResponse,
  ActionLogResponse,
} from "./types";

/* ----- Real-time / cockpit ----- */

export const getCockpitNow = () => getJson<CockpitNow>("/cockpit/now");
export const getSchedulerTimeline = () => getJson<SchedulerTimeline>("/scheduler/timeline");
export const getDecisionsLatest = () =>
  getJson<DispatchDecisionsResponse>("/optimization/decisions/latest");
export const getMetrics = () => getJson<MetricsResponse>("/metrics");
export const getPvToday = () => getJson<PvTodayResponse>("/pv/today");
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
export const getExecutionToday = () =>
  getJson<ExecutionTodayResponse>("/execution/today");
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
export const getEnergyTodayCumulative = () =>
  getJson<TodayCumulativeResponse>("/energy/today-cumulative");
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

/* ----- Settings ----- */

export const getSettings = () => getJson<SettingsList>("/settings");

export const simulateBatch = (changes: Record<string, unknown>) =>
  postJson<SimulateBatchResponse>("/settings/batch/simulate", { changes });

export async function applyBatch(
  simulationId: string,
  changes: Array<{ key: string; value: unknown }>,
): Promise<ApplyBatchResponse> {
  const headers = new Headers({ "Content-Type": "application/json", "X-Simulation-Id": simulationId });
  const r = await hemFetch("/settings/batch", {
    method: "POST",
    body: JSON.stringify(changes),
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
