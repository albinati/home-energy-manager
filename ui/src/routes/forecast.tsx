import { useMemo } from "preact/hooks";
import { useFetch } from "../lib/poll";
import {
  getWeather,
  getExecutionToday,
  getAgileToday,
  getPvCalibration,
} from "../lib/endpoints";
import { Card } from "../components/common/Card";
import { Pill } from "../components/common/Pill";
import { Spinner } from "../components/common/Spinner";
import { ForecastChart } from "../components/forecast/ForecastChart";
import { chartTheme, baseOption, echarts } from "../lib/charts";
import { hhmm } from "../lib/format";
import type {
  WeatherResponse,
  ExecutionTodayResponse,
  AgileDayResponse,
} from "../lib/types";
import "../components/forecast/forecast.css";

const CHART_GROUP = "forecast";

// Connect grouped charts so hover lines up across panels.
function ensureGroupConnected() {
  // echarts.connect is idempotent for the same group string.
  echarts.connect(CHART_GROUP);
}

export default function Forecast() {
  const weather = useFetch(getWeather, []);
  const execution = useFetch(getExecutionToday, []);
  const agile = useFetch(getAgileToday, []);
  const cal = useFetch(getPvCalibration, []);

  const refreshAll = async () => {
    await Promise.all([weather.refresh(), execution.refresh(), agile.refresh(), cal.refresh()]);
  };

  const pvOption = useMemo(
    () => buildPvOption(weather.data, execution.data),
    [weather.data, execution.data],
  );
  const tempOption = useMemo(
    () => buildTempOption(weather.data, execution.data),
    [weather.data, execution.data],
  );
  const priceOption = useMemo(
    () => buildPriceOption(agile.data),
    [agile.data],
  );

  // Connect group once option-data is in.
  if (typeof window !== "undefined") {
    ensureGroupConnected();
  }

  const loading = weather.loading || execution.loading || agile.loading;
  const calFactor = cal.data?.factor;

  return (
    <div class="forecast-page">
      <header class="forecast-header">
        <div>
          <div class="forecast-eyebrow">Today</div>
          <h1>Forecast vs actuals</h1>
          <p class="forecast-sub">
            Quartz / Open-Meteo predictions overlaid on what the inverter actually did.
            Hover any chart to align across panels.
          </p>
        </div>
        <div class="forecast-actions">
          <div class="forecast-meta">
            {calFactor != null && (
              <Pill
                tone={Math.abs(calFactor - 1) < 0.1 ? "ok" : "warn"}
                title={`PV calibration factor over the last ${cal.data?.window_days || "?"} days`}
              >
                PV cal {calFactor.toFixed(2)}×
              </Pill>
            )}
            {weather.data?.daikin?.outdoor_temp != null && (
              <Pill tone="dim" title="Live Daikin outdoor sensor">
                Outdoor {weather.data.daikin.outdoor_temp.toFixed(1)}°C
              </Pill>
            )}
          </div>
          <button class="btn" onClick={refreshAll} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </button>
        </div>
      </header>

      <Card
        title={
          <div class="forecast-panel-title">
            <h3>Solar — kW</h3>
            <span class="muted">Forecast vs realised</span>
          </div>
        }
        subtitle="Quartz/Open-Meteo modelled solar curve against Fox realised. Calibration factor scales the forecast over a 14-day rolling window."
      >
        {weather.error || execution.error ? (
          <p class="muted">{(weather.error || execution.error)?.message}</p>
        ) : weather.data || execution.data ? (
          <ForecastChart option={pvOption} group={CHART_GROUP} height={240} />
        ) : (
          <Spinner label="Loading forecast…" />
        )}
      </Card>

      <Card
        title={
          <div class="forecast-panel-title">
            <h3>Outdoor temperature — °C</h3>
            <span class="muted">Open-Meteo vs Daikin sensor</span>
          </div>
        }
        subtitle="The 10 km grid Open-Meteo runs on tends to under-estimate W4 1DZ overnight; FORECAST_NIGHT_TEMP_BIAS_C corrects the LP-side."
      >
        {weather.data || execution.data ? (
          <ForecastChart option={tempOption} group={CHART_GROUP} height={220} />
        ) : (
          <Spinner label="Loading temperature…" />
        )}
      </Card>

      <Card
        title={
          <div class="forecast-panel-title">
            <h3>Octopus Agile — p/kWh</h3>
            <span class="muted">Import (bars) + export (line)</span>
          </div>
        }
        subtitle="Negative slots are paid imports; peak slots above the dashed line drive arbitrage decisions."
      >
        {agile.error ? (
          <p class="muted">{agile.error.message}</p>
        ) : agile.data ? (
          <ForecastChart option={priceOption} group={CHART_GROUP} height={220} />
        ) : (
          <Spinner label="Loading rates…" />
        )}
      </Card>
    </div>
  );
}

/* -------- option builders -------- */

// Reduce ISO timestamp to local HH:MM for category axis labels.
function tLabel(iso: string): string { return hhmm(iso); }

// Merge two same-axis series under a single hour set. Returns the union of
// timestamps sorted ascending as ISO strings + lookup maps.
function unionTimestamps(arrays: Array<Array<{ time?: string; slot_time_utc?: string }>>): string[] {
  const set = new Set<string>();
  for (const arr of arrays) {
    for (const it of arr) {
      const t = (it.time || it.slot_time_utc) as string | undefined;
      if (t) set.add(t);
    }
  }
  return [...set].sort();
}

function buildPvOption(
  weather: WeatherResponse | null,
  exec: ExecutionTodayResponse | null,
): Record<string, unknown> {
  const t = chartTheme();
  const base = baseOption();

  const forecast = weather?.forecast || [];
  const realised = exec?.slots || [];
  const xs = unionTimestamps([forecast, realised]);
  const forecastMap = new Map(forecast.map((f) => [f.time, f.pv_kw]));
  // exec slots carry pv_kwh per 30-min slot; convert to kW (avg over 0.5h) by *2.
  const realisedMap = new Map(
    realised.map((s) => [s.slot_time_utc, s.pv_kwh != null ? s.pv_kwh * 2 : null]),
  );

  return {
    ...base,
    legend: { ...(base.legend as object), data: ["Forecast", "Realised"] },
    xAxis: { ...(base.xAxis as object), data: xs.map(tLabel) },
    yAxis: { ...(base.yAxis as object), name: "kW", nameTextStyle: { color: t.textDim, fontSize: 10 } },
    series: [
      {
        name: "Forecast",
        type: "line",
        smooth: true,
        symbol: "none",
        lineStyle: { color: t.pv, width: 2, type: "dashed" },
        areaStyle: { color: t.pv, opacity: 0.05 },
        data: xs.map((x) => forecastMap.get(x) ?? null),
      },
      {
        name: "Realised",
        type: "line",
        smooth: false,
        symbol: "none",
        lineStyle: { color: t.pv, width: 2 },
        areaStyle: { color: t.pv, opacity: 0.18 },
        data: xs.map((x) => realisedMap.get(x) ?? null),
      },
    ],
  };
}

function buildTempOption(
  weather: WeatherResponse | null,
  exec: ExecutionTodayResponse | null,
): Record<string, unknown> {
  const t = chartTheme();
  const base = baseOption();

  const forecast = weather?.forecast || [];
  const realised = exec?.slots || [];
  const xs = unionTimestamps([forecast, realised]);
  const forecastMap = new Map(forecast.map((f) => [f.time, f.temp_c]));
  const realisedMap = new Map(realised.map((s) => [s.slot_time_utc, s.outdoor_temp_c ?? null]));

  return {
    ...base,
    legend: { ...(base.legend as object), data: ["Open-Meteo", "Daikin sensor"] },
    xAxis: { ...(base.xAxis as object), data: xs.map(tLabel) },
    yAxis: { ...(base.yAxis as object), name: "°C", nameTextStyle: { color: t.textDim, fontSize: 10 } },
    series: [
      {
        name: "Open-Meteo",
        type: "line",
        smooth: true,
        symbol: "none",
        lineStyle: { color: t.accent, width: 2, type: "dashed" },
        data: xs.map((x) => forecastMap.get(x) ?? null),
      },
      {
        name: "Daikin sensor",
        type: "line",
        smooth: false,
        symbol: "circle",
        symbolSize: 4,
        lineStyle: { color: t.warn, width: 2 },
        itemStyle: { color: t.warn },
        data: xs.map((x) => realisedMap.get(x) ?? null),
      },
    ],
  };
}

function buildPriceOption(agile: AgileDayResponse | null): Record<string, unknown> {
  const t = chartTheme();
  const base = baseOption();

  const imp = agile?.import || [];
  const exp = agile?.export || [];
  const xs = unionTimestamps([imp, exp]);
  const importMap = new Map(imp.map((r) => [r.slot_time_utc, r.value_inc_vat]));
  const exportMap = new Map(exp.map((r) => [r.slot_time_utc, r.value_inc_vat]));

  // Threshold guesses for visual reference — real thresholds come from /metrics
  // but for the chart axis lines, sensible defaults work.
  const peakP = 28;
  const cheapP = 12;

  return {
    ...base,
    legend: { ...(base.legend as object), data: ["Import", "Export"] },
    xAxis: { ...(base.xAxis as object), data: xs.map(tLabel) },
    yAxis: { ...(base.yAxis as object), name: "p/kWh", nameTextStyle: { color: t.textDim, fontSize: 10 } },
    series: [
      {
        name: "Import",
        type: "bar",
        barWidth: "75%",
        data: xs.map((x) => {
          const v = importMap.get(x);
          if (v == null) return null;
          let color = t.textDim;
          if (v < 0) color = t.neg;
          else if (v < cheapP) color = t.cheap;
          else if (v >= peakP) color = t.peak;
          return { value: v, itemStyle: { color } };
        }),
        markLine: {
          silent: true,
          symbol: "none",
          lineStyle: { color: t.border, type: "dashed", opacity: 0.6 },
          data: [
            { yAxis: peakP, label: { color: t.peak, formatter: `peak ${peakP}p` } },
            { yAxis: cheapP, label: { color: t.cheap, formatter: `cheap ${cheapP}p` } },
            { yAxis: 0, label: { color: t.textDim, formatter: "0" } },
          ],
        },
      },
      {
        name: "Export",
        type: "line",
        smooth: false,
        symbol: "none",
        lineStyle: { color: t.exportColor, width: 2 },
        data: xs.map((x) => exportMap.get(x) ?? null),
      },
    ],
  };
}
