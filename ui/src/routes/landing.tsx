import { useEffect, useState } from "preact/hooks";
import { usePoll, useFetch } from "../lib/poll";
import {
  getCockpitNow,
  getMetrics,
  getAgileToday,
  getWeather,
  getSchedulerTimeline,
  getExecutionToday,
  getAttributionDay,
  getEnergyReport,
  getEnergyMonthly,
  getDaikinStatus,
  getDaikinQuota,
  getTariffDashboard,
} from "../lib/endpoints";
import { Widget } from "../components/common/Widget";
import { Spinner } from "../components/common/Spinner";
import { RefreshAction } from "../components/common/RefreshAction";
import { LivePowerWidget } from "../components/cockpit/LivePowerWidget";
import { TariffWidget } from "../components/cockpit/TariffWidget";
import { Hero } from "../components/home/Hero";
import { ExportsWidget } from "../components/home/ExportsWidget";
import { TodayBillWidget } from "../components/home/TodayBillWidget";
import { LifetimeWidget } from "../components/home/LifetimeWidget";
import { EfficiencyWidget } from "../components/home/EfficiencyWidget";
import { HeatingWidget } from "../components/home/HeatingWidget";
import { TariffComparisonWidget } from "../components/home/TariffComparisonWidget";
import type { MonthlyEnergy } from "../lib/types";
import "../components/home/home.css";

function lastMonths(n: number): string[] {
  const now = new Date();
  const out: string[] = [];
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    out.push(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`);
  }
  return out;
}

function useMonthlyHistory(n: number) {
  const [data, setData] = useState<MonthlyEnergy[]>([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all(lastMonths(n).map((m) => getEnergyMonthly(m).catch(() => null))).then((r) => {
      if (!alive) return;
      setData(r.filter((x): x is MonthlyEnergy => !!x));
      setLoading(false);
    });
    return () => { alive = false; };
  }, [n]);
  return { data, loading };
}

export default function Landing() {
  // Cache-only endpoints — poll while the tab is visible (usePoll auto-pauses
  // via visibilitychange). All of these read SQLite/memory, no cloud calls.
  const now = usePoll(getCockpitNow, 20_000);
  const metrics = usePoll(getMetrics, 5 * 60_000);
  const timeline = usePoll(getSchedulerTimeline, 5 * 60_000);

  // Fetch-once endpoints — refresh on tab return, otherwise no churn.
  const agile = useFetch(getAgileToday, []);
  const weather = useFetch(getWeather, []);
  const execution = useFetch(getExecutionToday, []);
  const attribution = useFetch(() => getAttributionDay(), []);
  const report = useFetch(() => getEnergyReport(new Date().toISOString().slice(0, 10)), []);
  const monthly = useMonthlyHistory(6);
  // Daikin cached read — no refresh=true, so no live cloud call (30-min cache TTL).
  const daikin = useFetch(getDaikinStatus, []);
  const daikinQuota = useFetch(getDaikinQuota, []);
  // Tariff comparison vs Octopus catalogue + BG Fixed v58.
  const tariffDash = useFetch(() => getTariffDashboard(1, "monthly", 8), []);

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
      <Hero metrics={metrics.data} metricsLoading={metrics.loading} cockpit={data} agile={agile.data} />

      <div class="widget-grid">
        <Widget title="Live power" icon="⚡" tone="power" size="large"
                badge={data.now_utc ? new Date(data.now_utc).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : undefined}>
          <LivePowerWidget state={s} cockpit={data} timeline={timeline.data} execution={execution.data} />
        </Widget>

        <Widget title="Today's bill" icon="💰" tone="savings" size="medium"
                action={<RefreshAction onRefresh={report.refresh} loading={report.loading} />}>
          <TodayBillWidget report={report.data} reportLoading={report.loading} metrics={metrics.data} execution={execution.data} />
        </Widget>

        <Widget title="Efficiency" icon="🎯" tone="plan" size="medium">
          <EfficiencyWidget metrics={metrics.data} loading={metrics.loading} />
        </Widget>

        <Widget title="Tariff comparison" icon="📊" tone="tariff" size="wide"
                badge={tariffDash.data?.usage?.total_days ? `last ${tariffDash.data.usage.total_days}d` : undefined}
                action={<RefreshAction onRefresh={tariffDash.refresh} loading={tariffDash.loading} />}>
          <TariffComparisonWidget dashboard={tariffDash.data} dashboardLoading={tariffDash.loading} metrics={metrics.data} />
        </Widget>

        <Widget title="Today's tariff" icon="💷" tone="tariff" size="large">
          <TariffWidget agile={agile.data} now={data} metrics={metrics.data} />
        </Widget>

        <Widget title="Heating" icon="♨" tone="thermal" size="medium"
                action={<RefreshAction onRefresh={() => { void daikin.refresh(); void daikinQuota.refresh(); }} loading={daikin.loading} title="Re-fetch Daikin (cached server-side ~30min)" />}>
          <HeatingWidget state={s} daikin={daikin.data} daikinQuota={daikinQuota.data} report={report.data} weather={weather.data} execution={execution.data} />
        </Widget>

        <Widget title="Exports" icon="📤" tone="savings" size="medium"
                action={<RefreshAction onRefresh={() => { void attribution.refresh(); void report.refresh(); }} loading={attribution.loading || report.loading} />}>
          <ExportsWidget now={data} yesterday={attribution.data} report={report.data} monthly={monthly.data} />
        </Widget>

        <Widget title="Lifetime" icon="🏆" tone="savings" size="medium"
                badge={monthly.data.length > 0 ? `${monthly.data.length} mo on Agile` : undefined}>
          <LifetimeWidget monthly={monthly.data} monthlyLoading={monthly.loading} />
        </Widget>
      </div>
    </div>
  );
}
