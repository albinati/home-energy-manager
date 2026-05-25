import { useMemo } from "preact/hooks";
import { usePoll, useFetch } from "../lib/poll";
import {
  getCockpitNow,
  getSchedulerTimeline,
  getDecisionsLatest,
  getExecutionToday,
} from "../lib/endpoints";
import { Card } from "../components/common/Card";
import { Pill } from "../components/common/Pill";
import { Spinner } from "../components/common/Spinner";
import { PowerFlow } from "../components/cockpit/PowerFlow";
import { BatteryWidget } from "../components/cockpit/BatteryWidget";
import { DispatchReason } from "../components/cockpit/DispatchReason";
import { NextTransitionStrip } from "../components/cockpit/NextTransitionStrip";
import { hhmm, kw, kwh, tempC, relTime } from "../lib/format";
import type { DispatchDecisionsResponse } from "../lib/types";
import "../components/cockpit/cockpit.css";

export default function Cockpit() {
  const now = usePoll(getCockpitNow, 20_000);
  const timeline = usePoll(getSchedulerTimeline, 5 * 60_000);
  const decisions = usePoll(getDecisionsLatest, 60_000);
  const execution = useFetch(getExecutionToday, []);

  const currentReason = useMemo(
    () => extractCurrentReason(now.data?.now_utc, decisions.data),
    [now.data?.now_utc, decisions.data],
  );

  if (now.loading && !now.data) {
    return (
      <div class="cockpit-page">
        <Spinner label="Loading live state…" />
      </div>
    );
  }
  if (now.error && !now.data) {
    return (
      <div class="cockpit-page">
        <h1 class="cockpit-title">Cockpit</h1>
        <p class="muted">Failed to load: {now.error.message}</p>
        <button class="btn" onClick={() => now.refresh()}>Retry</button>
      </div>
    );
  }

  const data = now.data;
  if (!data) return null;
  const s = data.state;

  return (
    <div class="cockpit-page">
      <header class="cockpit-header">
        <div>
          <div class="cockpit-eyebrow">Live</div>
          <h1 class="cockpit-title">Cockpit</h1>
        </div>
        <div class="cockpit-now">
          Updated {hhmm(data.now_utc)} · {relTime(data.now_utc)}
        </div>
      </header>

      <div class="cockpit-grid">
        <Card title="Power flow" subtitle="Live inverter telemetry, cached. PV → Battery / House → Grid.">
          <PowerFlow state={s} />
          <div class="freshness-ribbon">
            {data.freshness && Object.entries(data.freshness).map(([source, f]) => (
              <Pill key={source} tone={f.stale ? "warn" : "dim"} title={`${source} fetched ${relTime(f.fetched_at_utc)}`}>
                {source} {formatAge(f.age_s)}
              </Pill>
            ))}
          </div>
        </Card>

        <Card title="Battery" subtitle="State of charge, today's range, next planned event.">
          <BatteryWidget state={s} timeline={timeline.data} execution={execution.data} />
        </Card>
      </div>

      <Card title="What's happening now" subtitle="Slot kind, current prices, and the LP's reasoning.">
        <DispatchReason now={data} decisionReason={currentReason} />
      </Card>

      <Card title="Live metrics">
        <div class="metrics-tiles">
          <Tile label="Solar" value={kw(s.solar_kw)} />
          <Tile label="House load" value={kw(s.load_kw)} />
          <Tile label="Grid" value={kw(Math.abs(s.grid_kw))} sub={s.grid_kw >= 0 ? "importing" : "exporting"} />
          <Tile label="Battery" value={kw(Math.abs(s.battery_kw))} sub={s.battery_kw > 0 ? "charging" : s.battery_kw < 0 ? "discharging" : "idle"} />
          <Tile label="Indoor" value={tempC(s.indoor_c)} />
          <Tile label="Tank" value={tempC(s.tank_c)} />
          <Tile label="Outdoor LWT" value={tempC(s.lwt_c)} />
          <Tile label="Daikin" value={s.daikin_mode || "—"} />
        </div>
      </Card>

      <Card
        title="Next dispatch transitions"
        subtitle={
          timeline.data?.plan_date
            ? `From plan ${timeline.data.plan_date}, run at ${hhmm(timeline.data.run_at)}.`
            : "Loading scheduler timeline…"
        }
        class="cockpit-strip-card"
      >
        <NextTransitionStrip timeline={timeline.data} />
      </Card>

      <div class="muted" style="text-align:center; font-size:var(--font-xs)">
        Polling every 20s. State and decisions auto-refresh; pause by hiding the tab.
        Reserved capacity {kwh(s.soc_kwh)} / battery driving the plan.
      </div>
    </div>
  );
}

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div class="metric-tile">
      <div class="metric-tile-label">{label}</div>
      <div class="metric-tile-value">{value}</div>
      {sub && <div class="metric-tile-sub">{sub}</div>}
    </div>
  );
}

function formatAge(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

function extractCurrentReason(
  nowUtc: string | undefined,
  decisions: DispatchDecisionsResponse | null,
): string | null {
  if (!nowUtc || !decisions?.decisions || decisions.decisions.length === 0) return null;
  // Find the decision whose slot covers nowUtc (30-min slots assumed).
  const t = Date.parse(nowUtc);
  if (!Number.isFinite(t)) return null;
  for (let i = decisions.decisions.length - 1; i >= 0; i--) {
    const d = decisions.decisions[i];
    if (d.slot_time_utc && Date.parse(d.slot_time_utc) <= t) {
      return d.reason || null;
    }
  }
  return null;
}
