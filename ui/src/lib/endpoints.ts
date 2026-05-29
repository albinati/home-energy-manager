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
  TariffDashboardResponse,
  PeriodInsightsResponse,
  DaikinConsumptionResponse,
  PvTodayResponse,
  OptimizationInputsResponse,
  ActionResult,
  DaikinOperationMode,
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
export const getDaikinQuota = () => getJson<ApiQuotaResponse>("/daikin/quota");
export const getFoxQuota = () => getJson<ApiQuotaResponse>("/foxess/quota");

/* ----- Daikin controls (writes — require DAIKIN_CONTROL_MODE=active) -----
   The UI shows its own confirm dialog, then sends skip_confirmation:true so
   the backend doesn't return a separate pending-action step. A 409
   PassiveModeLocked surfaces as a HemApiError the caller can toast. */
export const setTankTemperature = (temperature: number) =>
  postJson<ActionResult>("/daikin/tank-temperature", { temperature });
export const setTankPower = (on: boolean) =>
  postJson<ActionResult>("/daikin/tank-power", { on, skip_confirmation: true });
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

// POST /tariffs/dashboard — Octopus-tariff comparison engine, includes
// standing charges + export earnings in the per-tariff costs.
export const getTariffDashboard = (months_back = 1, granularity: "daily" | "weekly" | "monthly" = "monthly", max_tariffs = 8) =>
  postJson<TariffDashboardResponse>("/tariffs/dashboard", { months_back, granularity, max_tariffs });

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
