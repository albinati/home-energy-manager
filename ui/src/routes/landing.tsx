import { usePoll, useFetch } from "../lib/poll";
import {
  getCockpitNow,
  getMetrics,
  getAgileToday,
  getWeather,
  getSchedulerTimeline,
  getExecutionToday,
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
import { Hero } from "../components/home/Hero";
import { ExportsWidget } from "../components/home/ExportsWidget";
import type { DispatchDecisionsResponse } from "../lib/types";
import "../components/home/home.css";

export default function Landing() {
  const now = usePoll(getCockpitNow, 20_000);
  const metrics = useFetch(getMetrics, []);
  const agile = useFetch(getAgileToday, []);
  const weather = useFetch(getWeather, []);
  const timeline = useFetch(getSchedulerTimeline, []);
  const execution = useFetch(getExecutionToday, []);
  const decisions = useFetch(getDecisionsLatest, []);
  const attribution = useFetch(() => getAttributionDay(), []);

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
