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
} from "../lib/endpoints";
import { Card } from "../components/common/Card";
import { Spinner } from "../components/common/Spinner";
import { PowerFlow } from "../components/cockpit/PowerFlow";
import { BatteryWidget } from "../components/cockpit/BatteryWidget";
import { DispatchReason } from "../components/cockpit/DispatchReason";
import { TariffStrip } from "../components/cockpit/TariffStrip";
import { ComingUp } from "../components/home/ComingUp";
import { SavingsSparkline } from "../components/home/SavingsSparkline";
import { gbpSigned, tempC } from "../lib/format";
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

// Deferred — non-blocking. Renders progressively without holding the page back.
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
  const monthly = useMonthlyHistory(3);

  if (now.loading && !now.data) {
    return (
      <div class="home">
        <Spinner label="Loading dashboard…" />
      </div>
    );
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
  const daily = metrics.data?.pnl?.daily;
  const todayDelta = daily?.delta_vs_svt_pounds ?? null;
  const monthDelta = metrics.data?.pnl?.monthly?.delta_vs_svt_pounds ?? null;
  const weekDelta = metrics.data?.pnl?.weekly?.delta_vs_svt_pounds ?? null;
  const currentReason = extractCurrentReason(data.now_utc, decisions.data);

  return (
    <div class="home">
      {/* HERO — Today's savings */}
      <section class="home-hero" aria-label="Today">
        <div>
          <div class="home-hero-eyebrow"><strong>Today</strong> · saved vs Standard Variable Tariff</div>
          <div class={`home-hero-cost ${todayDelta == null ? "home-hero-cost-neutral" : todayDelta >= 0 ? "home-hero-cost-positive" : "home-hero-cost-negative"}`}>
            {todayDelta == null ? (metrics.loading ? <SkelText w="6rem" /> : "—") : gbpSigned(todayDelta)}
          </div>
          {daily?.delta_vs_fixed_pounds != null && (
            <div class="home-hero-delta">
              vs fixed tariff: <strong class={daily.delta_vs_fixed_pounds >= 0 ? "" : "neg"}>{gbpSigned(daily.delta_vs_fixed_pounds)}</strong>
            </div>
          )}
        </div>
        <aside class="home-hero-aside">
          <div class="home-hero-aside-row">
            <span class="home-hero-aside-label">This week</span>
            <span class="home-hero-aside-value">{weekDelta != null ? gbpSigned(weekDelta) : metrics.loading ? <SkelText w="4rem" /> : "—"}</span>
          </div>
          <div class="home-hero-aside-row">
            <span class="home-hero-aside-label">This month</span>
            <span class="home-hero-aside-value">{monthDelta != null ? gbpSigned(monthDelta) : metrics.loading ? <SkelText w="4rem" /> : "—"}</span>
          </div>
          <div class="home-hero-aside-row">
            <span class="home-hero-aside-label">Mode</span>
            <span class="home-hero-aside-value">{s.daikin_mode || "—"}</span>
          </div>
        </aside>
      </section>

      {/* POWER FLOW */}
      <Card title="Live power flow">
        <PowerFlow state={s} />
      </Card>

      {/* BATTERY */}
      <Card title="Battery" subtitle="State of charge, today's range, next planned event.">
        <BatteryWidget state={s} timeline={timeline.data} execution={execution.data} />
      </Card>

      {/* WHAT'S HAPPENING NOW (dispatch + tariff strip combined) */}
      <Card title="Right now" subtitle="Slot kind, prices, and what the LP is doing.">
        <DispatchReason now={data} decisionReason={currentReason} />
      </Card>

      <Card title="Today's tariff" subtitle="Half-hourly Octopus Agile import prices. Marker = current slot.">
        <TariffStrip
          agile={agile.data}
          cheapP={data.thresholds?.cheap_p ?? 12}
          peakP={data.thresholds?.peak_p ?? 28}
          nowUtc={data.now_utc}
        />
      </Card>

      {/* COMING UP */}
      <Card title="Coming up" subtitle="Next interesting events on today's price + solar horizon.">
        <ComingUp
          agile={agile.data}
          weather={weather.data}
          cheapP={data.thresholds?.cheap_p ?? 12}
          peakP={data.thresholds?.peak_p ?? 28}
          nowUtc={data.now_utc}
        />
      </Card>

      {/* THERMAL — small footer row, low-priority data */}
      <Card title="Thermal" pad="tight" variant="subtle">
        <div class="home-thermal-row">
          <span><strong>{tempC(s.tank_c, 0)}</strong> tank</span>
          <span><strong>{tempC(s.indoor_c, 0)}</strong> indoor</span>
          <span><strong>{tempC(s.lwt_c, 0)}</strong> LWT</span>
        </div>
      </Card>

      {/* SAVINGS SPARKLINE — deferred, fills in async */}
      <Card title="Monthly savings vs SVT" subtitle={`Last ${monthly.data.length || 3} months. Green = Agile beat SVT.`}>
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
              <Spinner size="sm" label="loading history…" />
            ) : (
              <SavingsSparkline monthly={monthly.data} />
            )}
          </div>
        </div>
      </Card>
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
