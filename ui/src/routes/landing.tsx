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
  getExportOpportunity,
  getApplianceSuggestions,
  getApplianceJobs,
  getAppliances,
} from "../lib/endpoints";
import { Widget } from "../components/common/Widget";
import { Icon } from "../components/common/Icon";
import { Spinner } from "../components/common/Spinner";
import { RefreshCountdown } from "../components/common/RefreshCountdown";
import { PeriodNavigator } from "../components/shell/PeriodNavigator";
import { usePeriod, periodFetchOpts } from "../lib/period";
import { LivePowerWidget } from "../components/cockpit/LivePowerWidget";
import { Hero } from "../components/home/Hero";
import { HeatingWidget } from "../components/home/HeatingWidget";
import { PlanWidget } from "../components/home/PlanWidget";
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
  // Export opportunity (SEG-vs-Agile money left on the table) — deferred, slow.
  const exportOppy = useFetch(() => (deferred ? getExportOpportunity(60) : Promise.resolve(null)), [deferred]);
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
  // Fox inverter mode for the Plan widget — only surface the pill when actively
  // forcing the battery (SelfUse is the resting state).
  const foxMode = data.current_slot?.fox_mode ?? undefined;
  const foxActive = foxMode ? !["selfuse", "self_use", "idle", "—", ""].includes(foxMode.toLowerCase()) : false;
  const liveTime = data.now_utc
    ? new Date(data.now_utc).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })
    : undefined;

  return (
    <div class="home">
      {/* Narrow screens only — wide screens get the chrome variant in the
          sticky TopNav (redesign P4c). Same global signal either way. */}
      <PeriodNavigator variant="page" />
      <Hero metrics={metrics.data} metricsLoading={metrics.loading} cockpit={data} agile={agile.data} monthly={monthly.data}
            period={periodInsights.data} periodState={period}
            periodLoading={periodInsights.loading} todayCum={todayCum.data}
            weather={weather.data} pv={pvToday.data} />

      {/* ── LIVE band (redesign P4) — split 50/50: live power flow + live heating
          (gauges + the heating-plan timeline). Wrapped in the accent-wash band
          so it reads as the always-now, self-driving surface that ignores the
          period selector above. */}
      <div class="widget-band live-band">
        <div class="live-band-head">
          <Icon name="power-live" size={13} /> Live · self-driving
          <span class="grow" />
          <span class="when">read-only · updates automatically{liveTime ? ` · ${liveTime}` : ""}</span>
        </div>
        <div class="widget-grid">
          <Widget title="Live power" icon={<Icon name="power-live" size={14} />} tone="power" size="half"
                  badge={liveTime}
                  action={<RefreshCountdown lastFetchAt={now.lastFetchAt} intervalMs={now.intervalMs} loading={now.loading} onRefresh={() => void now.refresh()} />}>
            <LivePowerWidget state={s} cockpit={data} timeline={timeline.data} execution={execution.data} agile={agile.data} metrics={metrics.data} dhwSchedule={dhwSched.data?.rows} todayCumulative={todayCum.data} />
          </Widget>

          <Widget title="Live heating" icon={<Icon name="heating" size={14} />} tone="thermal" size="half">
            <HeatingWidget state={s} daikin={daikin.data} daikinQuota={daikinQuota.data} report={report.data} weather={weather.data} execution={execution.data}
                           onRefresh={() => { void daikin.refresh(); void daikinQuota.refresh(); }} />
            <Suspense fallback={<Spinner label="Loading heating plan…" />}>
              <HeatingPlanWidget plan={heatingPlan.data} loading={heatingPlan.loading} />
            </Suspense>
          </Widget>
        </div>
      </div>

      {/* ── PLAN (full width). Weather moved into the hero (redesign P2). Plan =
          the committed dispatch (battery + heating LWT + tank + appliances). */}
      <div class="widget-grid widget-band">
        <Widget title="Plan" icon={<Icon name="schedule" size={14} />} tone="plan" size="wide">
          <PlanWidget timeline={timeline.data} dhwSchedule={dhwSched.data?.rows} heatingPlan={heatingPlan.data}
                      appliances={appliances.data?.appliances} applianceJobs={applianceJobs.data?.jobs}
                      applianceSuggestions={applianceSug.data?.suggestions}
                      nowUtc={data.now_utc} foxMode={foxMode} foxActive={foxActive} />
        </Widget>
      </div>

      {/* ── PERIOD scope divider (redesign P4) — everything below follows the
          day/week/month/year selector at the top, in contrast to the live band. */}
      <div class="scope scope--period">
        <span class="scope-dot" />
        Period · energy
        <span class="scope-when">follows the selector above</span>
      </div>

      {/* ── TIMELINES — Generation + Consumption, synced to the period navigator.
          Stacked full-width so a given time reads straight down the screen. ── */}
      <div class="widget-grid widget-band">
        <Widget title="Generation" icon={<Icon name="solar" size={14} />} tone="plan" size="wide">
          <Suspense fallback={<Spinner label="Loading generation…" />}>
            <GenerationWidget period={period} periodData={periodInsights.data} periodLoading={periodInsights.loading}
                              agile={agile.data} opportunity={exportOppy.data}
                              cheapP={metrics.data?.cheap_threshold_pence} peakP={metrics.data?.peak_threshold_pence} />
          </Suspense>
        </Widget>

        <Widget title="Consumption" icon={<Icon name="chart-bars" size={14} />} tone="power" size="wide">
          <Suspense fallback={<Spinner label="Loading consumption…" />}>
            <EnergyChartWidget execution={execution.data} pv={pvToday.data} />
          </Suspense>
        </Widget>
      </div>
    </div>
  );
}
