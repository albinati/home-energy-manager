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
} from "./types";

/* ----- Real-time / cockpit ----- */

export const getCockpitNow = () => getJson<CockpitNow>("/cockpit/now");
export const getSchedulerTimeline = () => getJson<SchedulerTimeline>("/scheduler/timeline");
export const getDecisionsLatest = () =>
  getJson<DispatchDecisionsResponse>("/optimization/decisions/latest");
export const getMetrics = () => getJson<MetricsResponse>("/metrics");
export const getDaikinStatus = () => getJson<DaikinDevice[]>("/daikin/status");
export const getDaikinQuota = () => getJson<ApiQuotaResponse>("/daikin/quota");
export const getFoxQuota = () => getJson<ApiQuotaResponse>("/foxess/quota");

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
