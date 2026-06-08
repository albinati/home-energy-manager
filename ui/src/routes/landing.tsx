import { useEffect, useState } from "preact/hooks";
import { lazy, Suspense } from "preact/compat";
import { usePoll, useFetch, useAfterPaint } from "../lib/poll";
import {
  getCockpitNow,
  getMetrics,
  getAgileToday,
  getWeather,
  getSchedulerTimeline,
  getExecutionToday,
  getEnergyReport,
  getEnergyMonthly,
  getEnergyPeriod,
  getDaikinStatus,
  getDaikinQuota,
  getPvToday,
  getDhwSchedule,
  getHeatingPlan,
  getEnergyTodayCumulative,
  getApplianceSuggestions,
  getApplianceJobs,
  getAppliances,
} from "../lib/endpoints";
import { Widget } from "../components/common/Widget";
import { Spinner } from "../components/common/Spinner";
import { RefreshCountdown } from "../components/common/RefreshCountdown";
import { PeriodNavigator } from "../components/shell/PeriodNavigator";
import { usePeriod, periodFetchOpts } from "../lib/period";
import { LivePowerWidget } from "../components/cockpit/LivePowerWidget";
import { Hero } from "../components/home/Hero";
import { HeatingWidget } from "../components/home/HeatingWidget";
import { WeatherWidget } from "../components/home/WeatherWidget";
import { ApplianceWidget } from "../components/home/ApplianceWidget";
import type { MonthlyEnergy } from "../lib/types";
import "../components/home/home.css";

// The four timeline widgets (Solar / Grid / Load / Heating) each own echarts
// (~193 KB gzip, shared chunk). Lazy-load so the hero + live band paint first
// and the charts stream in below the fold. Solar/Grid/Load sync to the period
// navigator; Heating keeps its own D-1/D/D+1 frame (not period-synced).
// Two synced energy timelines (each owns echarts, shared chunk):
//   • Generation — solar plan-vs-actual + grid export + Octopus EXPORT price.
//   • Consumption — the rich load breakdown (Base+Appliances+Heat-pump stacked,
//     forecast line, Daikin heating/tank over periods) + Octopus IMPORT price.
// Both sync to the period navigator (day = intraday detail + tariff zones;
// week/month/year = actuals bars). Heating keeps its own D-1/D/D+1 frame.
const GenerationWidget = lazy(() =>
  import("../components/home/GenerationWidget").then((m) => ({ default: m.GenerationWidget })),
);
const EnergyChartWidget = lazy(() =>
  import("../components/home/EnergyChartWidget").then((m) => ({ default: m.EnergyChartWidget })),
);
const HeatingPlanWidget = lazy(() =>
  import("../components/home/HeatingPlanWidget").then((m) => ({ default: m.HeatingPlanWidget })),
);

function lastMonths(n: number): string[] {
  const now = new Date();
  const out: string[] = [];
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    out.push(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`);
  }
  return out;
}

function useMonthlyHistory(n: number, enabled = true) {
  const [data, setData] = useState<MonthlyEnergy[]>([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    setLoading(true);
    Promise.all(lastMonths(n).map((m) => getEnergyMonthly(m).catch(() => null))).then((r) => {
      if (!alive) return;
      setData(r.filter((x): x is MonthlyEnergy => !!x));
      setLoading(false);
    });
    return () => { alive = false; };
  }, [n, enabled]);
  return { data, loading };
}

// Home dashboard, grouped into three semantic bands so the eye can skim:
//   1. LIVE   — what's happening right now (Live power, Heating)
//   2. MONEY  — £ in (Today's bill, Efficiency, Tariff comparison). Export
//      earnings (today + month) live in the Hero now, not a standalone widget.
//   3. ENERGY — kWh details over time (Energy flow chart, day/week/month/year)
// Bands are separated by spacing only (no labels) — minimal, Apple-style.
// "Today's tariff" widget removed: its info is already in the Hero (current
// import/export p/kWh) + Energy flow day-view (price line).
export default function Landing() {
  // Cache-only endpoints — poll while the tab is visible (usePoll auto-pauses
  // via visibilitychange). All of these read SQLite/memory, no cloud calls.
  const now = usePoll(getCockpitNow, 20_000);
  const metrics = usePoll(getMetrics, 5 * 60_000);
  const timeline = usePoll(getSchedulerTimeline, 5 * 60_000);
  const pvToday = usePoll(getPvToday, 5 * 60_000);

  // Non-critical, below-the-fold data waits until the browser is idle after
  // first paint — keeps the heavy Fox/Octopus rollups (lifetime, tariff) from
  // competing with the above-the-fold hero / live-power / plan data.
  const deferred = useAfterPaint();

  // Fetch-once endpoints — refresh on tab return, otherwise no churn.
  const agile = useFetch(getAgileToday, []);
  const weather = useFetch(getWeather, []);
  const execution = useFetch(getExecutionToday, []);
  const report = useFetch(() => getEnergyReport(new Date().toISOString().slice(0, 10)), []);
  // Lifetime rollup = 6 sequential /energy/monthly calls — deferred (it's a
  // small stats strip low in the hero, not the headline).
  const monthly = useMonthlyHistory(6, deferred);
  // Daikin cached read — no refresh=true, so no live cloud call (30-min cache TTL).
  const daikin = useFetch(getDaikinStatus, []);
  const daikinQuota = useFetch(getDaikinQuota, []);
  // DHW tank plan (today+tomorrow) — used by the Live-power tank badges.
  const dhwSched = useFetch(getDhwSchedule, []);
  // Heating-plan timeline (yesterday/today/tomorrow): outdoor temp + LWT offset
  // + tank + heating-on, recomputed per slot. Cache-only, poll while visible.
  const heatingPlan = usePoll(getHeatingPlan, 5 * 60_000);
  // Today's grid import/export so far (kWh + real £, credit on negative slots).
  const todayCum = usePoll(getEnergyTodayCumulative, 60_000);
  // Appliance status: registered list (once) + active jobs + cheapest-window
  // suggestions for idle machines. All optional/best-effort (SmartThings may be
  // unconfigured → the widget shows an empty/register hint).
  const appliances = useFetch(getAppliances, []);
  const applianceJobs = usePoll(() => getApplianceJobs({ limit: 20 }), 5 * 60_000);
  const applianceSug = usePoll(getApplianceSuggestions, 5 * 60_000);
  // The shared period navigator drives the Hero headline + cost breakdown +
  // energy chart + tariff comparison. Re-fetch whenever the selection changes.
  const period = usePeriod();
  const periodInsights = useFetch(
    () => getEnergyPeriod(period.gran, periodFetchOpts(period)),
    [period.gran, period.anchor],
  );

  if (now.loading && !now.data) {
    return <div class="home"><Spinner label="Loading dashboard…" /></div>;
  }
  if (!now.data) {
    return (
      <div class="home">
        <p class="muted">Cockpit unavailable: {now.error?.message || "no data"}</p>
        <button class="btn" onClick={() => now.refresh()}>Retry</button>
      </div>
    );
  }

  const data = now.data;
  const s = data.state;

  return (
    <div class="home">
      <PeriodNavigator />
      <Hero metrics={metrics.data} metricsLoading={metrics.loading} cockpit={data} agile={agile.data} monthly={monthly.data}
            period={periodInsights.data} periodState={period}
            periodLoading={periodInsights.loading} todayCum={todayCum.data} />

      {/* ── LIVE (glanceable now-state, up top — Apple/Tesla) ──────────
          These three IGNORE the period navigator: they're always "now". */}
      <div class="widget-grid widget-band">
        <Widget title="Live power" icon="⚡" tone="power" size="large"
                badge={data.now_utc ? new Date(data.now_utc).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : undefined}
                action={<RefreshCountdown lastFetchAt={now.lastFetchAt} intervalMs={now.intervalMs} loading={now.loading} onRefresh={() => void now.refresh()} />}>
          <LivePowerWidget state={s} cockpit={data} timeline={timeline.data} execution={execution.data} agile={agile.data} metrics={metrics.data} dhwSchedule={dhwSched.data?.rows} todayCumulative={todayCum.data} />
        </Widget>

        <Widget title="Heating" icon="♨" tone="thermal" size="medium">
          <HeatingWidget state={s} daikin={daikin.data} daikinQuota={daikinQuota.data} report={report.data} weather={weather.data} execution={execution.data}
                         onRefresh={() => { void daikin.refresh(); void daikinQuota.refresh(); }} />
        </Widget>

        <Widget title="Appliances" icon="🧺" tone="power" size="medium">
          <ApplianceWidget suggestions={applianceSug.data?.suggestions} jobs={applianceJobs.data?.jobs} appliances={appliances.data?.appliances} />
        </Widget>

        <Widget title="Weather" icon="⛅" tone="thermal" size="medium">
          <WeatherWidget weather={weather.data} pv={pvToday.data} />
        </Widget>
      </div>

      {/* ── TIMELINES — Generation + Consumption, both synced to the period
          navigator (navigate one → navigate all). Heating keeps its own
          D-1/D/D+1 frame. Day = forecast-vs-actual + cheap/peak/negative tariff
          zones + the Octopus export/import price slots; week/month/year =
          actuals bars (no historical intraday forecast). ─────────────────── */}
      <div class="widget-grid widget-band">
        <Widget title="Generation" icon="☀" tone="plan" size="wide">
          <Suspense fallback={<Spinner label="Loading generation…" />}>
            <GenerationWidget period={period} periodData={periodInsights.data} periodLoading={periodInsights.loading}
                              agile={agile.data}
                              cheapP={metrics.data?.cheap_threshold_pence} peakP={metrics.data?.peak_threshold_pence} />
          </Suspense>
        </Widget>

        <Widget title="Consumption" icon="📈" tone="power" size="wide">
          <Suspense fallback={<Spinner label="Loading consumption…" />}>
            <EnergyChartWidget execution={execution.data} pv={pvToday.data} />
          </Suspense>
        </Widget>

        <Widget title="Heating plan" icon="♨" tone="thermal" size="wide">
          <Suspense fallback={<Spinner label="Loading heating plan…" />}>
            <HeatingPlanWidget plan={heatingPlan.data} loading={heatingPlan.loading} />
          </Suspense>
        </Widget>
      </div>
    </div>
  );
}
