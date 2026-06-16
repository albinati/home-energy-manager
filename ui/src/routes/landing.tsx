import { useEffect } from "preact/hooks";
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
import { usePeriod, periodFetchOpts, periodScope, isCurrentPeriod } from "../lib/period";
import { LivePowerWidget } from "../components/cockpit/LivePowerWidget";
import { Hero } from "../components/home/Hero";
import { ForecastStrip } from "../components/home/ForecastStrip";
import { HeatingWidget } from "../components/home/HeatingWidget";
import { PlanMini } from "../components/home/PlanMini";
import { FeedbackPanel } from "../components/home/FeedbackPanel";
import { OperateCard } from "../components/home/OperateCard";
import { LifetimeStrip } from "../components/home/LifetimeStrip";
import { publishFreshness } from "../lib/freshness";
import { role } from "../lib/auth";
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
    { cacheKey: `energy:${period.gran}:${period.anchor}`, immutable: !isCurrentPeriod(period) },
  );

  // Publish the cockpit's per-source freshness map so the shell-level
  // AlertStrip can flag stale live data WITHOUT its own /cockpit/now poll.
  // Cleared on unmount — off this route the poll stops, and a frozen map
  // would let a "stale data" chip linger past its 2-min relevance window.
  useEffect(() => {
    publishFreshness(now.data?.freshness);
  }, [now.data]);
  useEffect(() => () => publishFreshness(null), []);

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

  const scope = periodScope(period);

  return (
    <div class="home">
      {/* Narrow screens only — wide screens get the chrome variant in the
          sticky TopNav (redesign P4c). Same global signal either way. */}
      <PeriodNavigator variant="page" />

      {/* ── PERIOD-VIEW scope (redesign) — names what the hero is scoped to.
          <h2> so the cockpit has a real document outline (period section). */}
      <h2 class="scope scope--period">
        <span class="scope-dot" aria-hidden="true" />
        {scope.scope} · period view
        {scope.date && <span class="scope-when">{scope.date}</span>}
      </h2>

      <Hero metrics={metrics.data} metricsLoading={metrics.loading} cockpit={data} agile={agile.data}
            period={periodInsights.data} periodState={period}
            periodLoading={periodInsights.loading} todayCum={todayCum.data}
            weather={weather.data} pv={pvToday.data} />

      {/* Days ahead — 4-day weather + solar outlook (temp, rain, PV kWh/day). */}
      <ForecastStrip weather={weather.data} />

      {/* ── LIVE scope + band (redesign) — the always-now, self-driving surface
          that ignores the period selector above. Live power = the animated
          flow + rates + battery, with its committed plan as the card foot;
          Live heating = the plan chart first, gauges demoted beneath it. */}
      <h2 class="scope scope--live">
        <span class="scope-dot" aria-hidden="true" />
        Live now
        <span class="scope-when">always now — ignores the period above</span>
      </h2>
      <div class="widget-band live-band">
        <div class="live-band-head">
          <Icon name="power-live" size={13} /> Live · self-driving
          <span class="grow" />
          <span class="when">read-only · updates automatically{liveTime ? ` · ${liveTime}` : ""}</span>
        </div>

        {/* Admin control cluster. Mount-gated on role so viewers never fire
            its admin-only GET /settings; role is a signal, so the unlock
            re-renders this route and mounts the card. */}
        {role.value === "admin" && <OperateCard
          appliances={appliances.data?.appliances}
          applianceJobs={applianceJobs.data?.jobs}
          onChanged={() => {
            void timeline.refresh();
            void heatingPlan.refresh();
            void applianceJobs.refresh();
            void applianceSug.refresh();
            void now.refresh();
          }}
        />}
        <div class="widget-grid">
          <Widget title="Live power" icon={<Icon name="power-live" size={14} />} tone="power" size="half"
                  badge={liveTime}
                  action={<RefreshCountdown lastFetchAt={now.lastFetchAt} intervalMs={now.intervalMs} loading={now.loading} onRefresh={() => void now.refresh()} />}>
            <LivePowerWidget state={s} cockpit={data} agile={agile.data} metrics={metrics.data} todayCumulative={todayCum.data} />
            <PlanMini groups={["battery", "appliances"]} timeline={timeline.data}
                      appliances={appliances.data?.appliances} applianceJobs={applianceJobs.data?.jobs}
                      applianceSuggestions={applianceSug.data?.suggestions}
                      nowUtc={data.now_utc} foxMode={foxMode} foxActive={foxActive} />
          </Widget>

          <Widget title="Live heating" icon={<Icon name="heating" size={14} />} tone="thermal" size="half">
            <HeatingWidget state={s} daikin={daikin.data} daikinQuota={daikinQuota.data} report={report.data} weather={weather.data} execution={execution.data}
                           onRefresh={() => { void daikin.refresh(); void daikinQuota.refresh(); }}>
              <Suspense fallback={<Spinner label="Loading heating plan…" />}>
                <HeatingPlanWidget plan={heatingPlan.data} loading={heatingPlan.loading} />
              </Suspense>
            </HeatingWidget>
            <PlanMini groups={["heating", "tank"]} timeline={timeline.data}
                      dhwSchedule={dhwSched.data?.rows} heatingPlan={heatingPlan.data}
                      nowUtc={data.now_utc} />
          </Widget>
        </div>
      </div>

      {/* ── PERIOD scope divider (redesign P4) — everything below follows the
          day/week/month/year selector in the chrome, in contrast to the live
          band. */}
      <div class="scope scope--period">
        <span class="scope-dot" />
        {scope.scope} · energy
        <span class="scope-when">follows the {period.gran} selector</span>
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

      {/* ── SELF-CHECK — is the system doing what we shipped it to do?
          Forecast provenance, PV accuracy, DHW budget vs measured, LWT gate;
          admins also get the recent-actions feed. The panel renders its own
          widget band — and nothing at all against an older API image. */}
      <FeedbackPanel pv={pvToday.data} />

      {/* Lifetime-on-Agile closing line (moved out of the hero — today-first).
          Self-defers its 6 monthly fetches until the browser is idle. */}
      <LifetimeStrip />
    </div>
  );
}
