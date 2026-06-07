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
  getFairCompare,
  getPvToday,
  getDhwSchedule,
  getHeatingPlan,
  getEnergyTodayCumulative,
  getApplianceSuggestions,
  getApplianceJobs,
  getAppliances,
} from "../lib/endpoints";
import { Link } from "wouter-preact";
import { Widget } from "../components/common/Widget";
import { Spinner } from "../components/common/Spinner";
import { RefreshCountdown } from "../components/common/RefreshCountdown";
import { PeriodNavigator } from "../components/shell/PeriodNavigator";
import { usePeriod, periodFetchOpts, periodLabel } from "../lib/period";
import { LivePowerWidget } from "../components/cockpit/LivePowerWidget";
import { Hero } from "../components/home/Hero";
import { HeatingWidget } from "../components/home/HeatingWidget";
import { WeatherWidget } from "../components/home/WeatherWidget";
import { ApplianceWidget } from "../components/home/ApplianceWidget";
import { gbp } from "../lib/format";
import type { MonthlyEnergy } from "../lib/types";
import "../components/home/home.css";

// The four timeline widgets (Solar / Grid / Load / Heating) each own echarts
// (~193 KB gzip, shared chunk). Lazy-load so the hero + live band paint first
// and the charts stream in below the fold. Solar/Grid/Load sync to the period
// navigator; Heating keeps its own D-1/D/D+1 frame (not period-synced).
const SolarWidget = lazy(() =>
  import("../components/home/SolarWidget").then((m) => ({ default: m.SolarWidget })),
);
const GridWidget = lazy(() =>
  import("../components/home/GridWidget").then((m) => ({ default: m.GridWidget })),
);
const LoadWidget = lazy(() =>
  import("../components/home/LoadWidget").then((m) => ({ default: m.LoadWidget })),
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
  // Fair tariff comparison for the selected period — a light summary for the
  // home link card; the full breakdown lives on the /insights tab. Deferred
  // (network: catalogue) so it doesn't compete with the above-the-fold data.
  const fairCmp = useFetch(
    () => (deferred ? getFairCompare(period.gran, period.anchor) : Promise.resolve(null)),
    [deferred, period.gran, period.anchor],
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

      {/* ── TIMELINES — Solar / Grid / Load, all synced to the period
          navigator (navigate one → navigate all). Heating keeps its own
          D-1/D/D+1 frame as the visual 4th. Day = forecast-vs-actual; week/
          month/year = actuals only (no historical intraday forecast). ─────── */}
      <div class="widget-grid widget-band">
        <Widget title="Solar" icon="☀" tone="plan" size="wide">
          <Suspense fallback={<Spinner label="Loading solar…" />}>
            <SolarWidget period={period} periodData={periodInsights.data} periodLoading={periodInsights.loading}
                         cheapP={metrics.data?.cheap_threshold_pence} peakP={metrics.data?.peak_threshold_pence} />
          </Suspense>
        </Widget>

        <Widget title="Grid" icon="🔌" tone="power" size="wide">
          <Suspense fallback={<Spinner label="Loading grid…" />}>
            <GridWidget period={period} periodData={periodInsights.data} periodLoading={periodInsights.loading}
                        cheapP={metrics.data?.cheap_threshold_pence} peakP={metrics.data?.peak_threshold_pence} />
          </Suspense>
        </Widget>

        <Widget title="Load" icon="📈" tone="power" size="wide">
          <Suspense fallback={<Spinner label="Loading load…" />}>
            <LoadWidget period={period} periodData={periodInsights.data} periodLoading={periodInsights.loading}
                        cheapP={metrics.data?.cheap_threshold_pence} peakP={metrics.data?.peak_threshold_pence} />
          </Suspense>
        </Widget>

        <Widget title="Heating plan" icon="♨" tone="thermal" size="wide">
          <Suspense fallback={<Spinner label="Loading heating plan…" />}>
            <HeatingPlanWidget plan={heatingPlan.data} loading={heatingPlan.loading} />
          </Suspense>
        </Widget>
      </div>

      {/* ── MONEY (bottom of page) — link to the full Insights tab ──── */}
      <div class="widget-grid widget-band">
        <Widget title="Tariff comparison" icon="📊" tone="savings" size="wide">
          <Link href="/insights" class="tariff-link-card">
            <div class="tariff-link-main">
              {(() => {
                const d = fairCmp.data;
                if (!d || !d.tariffs.length) {
                  return <span class="muted">Compare your usage against every tariff →</span>;
                }
                const winner = d.tariffs.find((r) => r.product_code === d.winner_product_code);
                const onBest = winner?.is_current;
                return onBest
                  ? <span>You're on the cheapest tariff for {periodLabel(period)} — <strong>{winner?.display_name}</strong>.</span>
                  : <span>Cheapest for {periodLabel(period)}: <strong>{winner?.display_name}</strong> — save <strong>{gbp(d.savings_vs_current_pounds)}</strong>.</span>;
              })()}
            </div>
            <span class="tariff-link-cta">Compare →</span>
          </Link>
        </Widget>
      </div>
    </div>
  );
}
