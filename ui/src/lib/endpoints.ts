import { getJson, putJson, postJson, del, hemFetch } from "./api";
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
  TariffDashboardResponse,
  SettingsList,
  SettingSpec,
  SimulateBatchResponse,
  ApplyBatchResponse,
  MetricsResponse,
} from "./types";

/* ----- Real-time / cockpit ----- */

export const getCockpitNow = () => getJson<CockpitNow>("/cockpit/now");
export const getSchedulerTimeline = () => getJson<SchedulerTimeline>("/scheduler/timeline");
export const getDecisionsLatest = () =>
  getJson<DispatchDecisionsResponse>("/optimization/decisions/latest");
export const getMetrics = () => getJson<MetricsResponse>("/metrics");

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

export const getEnergyReport = (date?: string) =>
  getJson<EnergyReport>(date ? `/energy/report?date=${encodeURIComponent(date)}` : "/energy/report");
export const getEnergyMonthly = (month: string) =>
  getJson<MonthlyEnergy>(`/energy/monthly?month=${encodeURIComponent(month)}`);
export const getAttributionDay = (date?: string) =>
  getJson<AttributionDay>(date ? `/attribution/day?date=${encodeURIComponent(date)}` : "/attribution/day");
export const getEnergyPeriod = (start: string, end: string, grouping = "daily") =>
  getJson<{ period?: string; total_cost_gbp?: number; metrics?: Array<Record<string, number | string>>; export_kwh?: number; export_revenue_gbp?: number }>(
    `/energy/period?start_date=${encodeURIComponent(start)}&end_date=${encodeURIComponent(end)}&grouping=${grouping}`,
  );
export const getTariffsDashboard = (body: {
  start_date: string;
  end_date: string;
  tariff_codes: string[];
}) => postJson<TariffDashboardResponse>("/tariffs/dashboard", body);

/* ----- Settings ----- */

export const getSettings = () => getJson<SettingsList>("/settings");
export const getSetting = (key: string) =>
  getJson<{ key: string; value: unknown }>(`/settings/${encodeURIComponent(key)}`);
export const putSetting = (key: string, value: unknown) =>
  putJson<SettingSpec>(`/settings/${encodeURIComponent(key)}`, { value });
export const deleteSetting = (key: string) =>
  del(`/settings/${encodeURIComponent(key)}`);

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
