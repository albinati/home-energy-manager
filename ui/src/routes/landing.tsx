import { useEffect, useState } from "preact/hooks";
import { usePoll, useFetch } from "../lib/poll";
import {
  getCockpitNow,
  getMetrics,
  getAgileToday,
  getWeather,
  getSchedulerTimeline,
  getExecutionToday,
  getEnergyMonthly,
  getDecisionsLatest,
  getAttributionDay,
} from "../lib/endpoints";
import { Widget } from "../components/common/Widget";
import { Spinner } from "../components/common/Spinner";
import { PowerFlow } from "../components/cockpit/PowerFlow";
import { BatteryWidget } from "../components/cockpit/BatteryWidget";
import { DispatchReason } from "../components/cockpit/DispatchReason";
import { TariffWidget } from "../components/cockpit/TariffWidget";
import { ThermalWidget } from "../components/cockpit/ThermalWidget";
import { ComingUp } from "../components/home/ComingUp";
import { SavingsSparkline } from "../components/home/SavingsSparkline";
import { Hero } from "../components/home/Hero";
import { ExportsWidget } from "../components/home/ExportsWidget";
import { gbpSigned } from "../lib/format";
import type { MonthlyEnergy, DispatchDecisionsResponse } from "../lib/types";
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
  const now = usePoll(getCockpitNow, 20_000);
  const metrics = useFetch(getMetrics, []);
  const agile = useFetch(getAgileToday, []);
  const weather = useFetch(getWeather, []);
  const timeline = useFetch(getSchedulerTimeline, []);
  const execution = useFetch(getExecutionToday, []);
  const decisions = useFetch(getDecisionsLatest, []);
  const attribution = useFetch(() => getAttributionDay(), []);
  const monthly = useMonthlyHistory(3);

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
  const currentReason = extractCurrentReason(data.now_utc, decisions.data);

  return (
    <div class="home">
      <Hero metrics={metrics.data} metricsLoading={metrics.loading} />

      {/* WIDGET GRID */}
      <div class="widget-grid">
        <Widget title="Live power" icon="⚡" tone="power" size="large" badge={data.now_utc ? new Date(data.now_utc).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : undefined}>
          <PowerFlow state={s} />
        </Widget>

        <Widget title="Battery" icon="🔋" tone="battery" size="medium">
          <BatteryWidget state={s} timeline={timeline.data} execution={execution.data} />
        </Widget>

        <Widget title="Today's tariff" icon="💷" tone="tariff" size="large">
          <TariffWidget agile={agile.data} now={data} />
        </Widget>

        <Widget title="Thermal" icon="♨" tone="thermal" size="medium">
          <ThermalWidget state={s} />
        </Widget>

        <Widget title="Right now" icon="🎯" tone="plan" size="large">
          <DispatchReason now={data} decisionReason={currentReason} />
        </Widget>

        <Widget title="Exports" icon="📤" tone="savings" size="medium">
          <ExportsWidget now={data} yesterday={attribution.data} />
        </Widget>

        <Widget title="Coming up" icon="📅" tone="coming" size="medium">
          <ComingUp
            agile={agile.data}
            weather={weather.data}
            cheapP={data.thresholds?.cheap_p ?? 12}
            peakP={data.thresholds?.peak_p ?? 28}
            nowUtc={data.now_utc}
          />
        </Widget>

        <Widget title="Monthly savings trend" icon="💚" tone="savings" size="large" badge={`last ${monthly.data.length || 3} mo`}>
          <div class="home-savings">
            <div class="home-savings-headline">
              <div class="home-savings-headline-value">
                {monthly.data.length > 0
                  ? gbpSigned(monthly.data[monthly.data.length - 1].savings_vs_svt_gbp ?? 0)
                  : monthly.loading ? <SkelText w="5rem" /> : "—"}
              </div>
              <div class="home-savings-headline-label">This month vs SVT</div>
            </div>
            <div class="home-savings-aside">
              {monthly.loading ? (
                <Spinner size="sm" label="loading…" />
              ) : (
                <SavingsSparkline monthly={monthly.data} />
              )}
            </div>
          </div>
        </Widget>
      </div>
    </div>
  );
}

function extractCurrentReason(nowUtc: string | undefined, decisions: DispatchDecisionsResponse | null): string | null {
  if (!nowUtc || !decisions?.decisions || decisions.decisions.length === 0) return null;
  const t = Date.parse(nowUtc);
  if (!Number.isFinite(t)) return null;
  for (let i = decisions.decisions.length - 1; i >= 0; i--) {
    const d = decisions.decisions[i];
    if (d.slot_time_utc && Date.parse(d.slot_time_utc) <= t) return d.reason || null;
  }
  return null;
}

function SkelText({ w }: { w: string }) {
  return <span class="skel-text" style={{ width: w }} />;
}
