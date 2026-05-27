import type { AgileTodayResponse, AgileDaySlotsResponse, WeatherResponse } from "../../lib/types";
import { hhmm } from "../../lib/format";

interface ComingUpProps {
  agile: AgileTodayResponse | null;
  agileTomorrow?: AgileDaySlotsResponse | null;
  weather: WeatherResponse | null;
  cheapP: number;
  peakP: number;
  nowUtc: string;
  horizonHours?: number;
}

type Event = {
  kind: "negative" | "cheap" | "peak" | "solar" | "tomorrow";
  when: string;
  title: string;
  sub: string;
  color: string;
  icon: string;
};

// 24h horizon by default. Bundles today's remaining events + a peek at
// tomorrow's published min/max if Octopus has released them.
export function ComingUp({ agile, agileTomorrow, weather, cheapP, peakP, nowUtc, horizonHours = 24 }: ComingUpProps) {
  const nowMs = Date.parse(nowUtc);
  const horizonMs = nowMs + horizonHours * 3600 * 1000;
  const events: Event[] = [];

  // --- Today's remaining slots ---
  if (agile?.import_slots) {
    const future = agile.import_slots
      .slice()
      .sort((a, b) => a.valid_from.localeCompare(b.valid_from))
      .filter((s) => Date.parse(s.valid_from) > nowMs);

    findRun(future, (s) => s.p < 0, (run) => {
      events.push({
        kind: "negative", when: run.first.valid_from,
        title: "Negative price", sub: `${run.first.p.toFixed(1)}p · ${run.count * 30} min`,
        color: "var(--neg-price)", icon: "🔵",
      });
    });
    findRun(future, (s) => s.p >= peakP, (run) => {
      events.push({
        kind: "peak", when: run.first.valid_from,
        title: "Peak price", sub: `${run.first.p.toFixed(1)}p · ${run.count * 30} min`,
        color: "var(--peak)", icon: "🟠",
      });
    });
    findRun(future, (s) => s.p < cheapP, (run) => {
      if (run.count < 4) return;
      events.push({
        kind: "cheap", when: run.first.valid_from,
        title: "Cheap window", sub: `${run.first.p.toFixed(1)}p · ${run.count * 30}+ min`,
        color: "var(--cheap)", icon: "🟢",
      });
    });
  }

  // --- Tomorrow's snapshot (if published) ---
  if (agileTomorrow?.slots && agileTomorrow.slots.length > 0) {
    let tMin = Infinity, tMax = -Infinity;
    let tMinSlot = "", tMaxSlot = "";
    for (const s of agileTomorrow.slots) {
      if (s.p < tMin) { tMin = s.p; tMinSlot = s.valid_from; }
      if (s.p > tMax) { tMax = s.p; tMaxSlot = s.valid_from; }
    }
    events.push({
      kind: "tomorrow", when: agileTomorrow.slots[0].valid_from,
      title: `Tomorrow ${tMin.toFixed(1)}p → ${tMax.toFixed(1)}p`,
      sub: `Cheapest at ${hhmm(tMinSlot)}, peak at ${hhmm(tMaxSlot)}`,
      color: "var(--accent)", icon: "📅",
    });
  }

  // --- Solar peak ---
  if (weather?.forecast) {
    const future = weather.forecast.filter((f) => Date.parse(f.time) > nowMs && Date.parse(f.time) < horizonMs);
    if (future.length > 0) {
      let peak = future[0];
      for (const f of future) if (f.pv_kw > peak.pv_kw) peak = f;
      if (peak.pv_kw > 0.5) {
        events.push({
          kind: "solar", when: peak.time,
          title: "Solar peak", sub: `${peak.pv_kw.toFixed(1)} kW (${peak.temp_c?.toFixed(0) ?? "—"}°C)`,
          color: "var(--pv)", icon: "☀",
        });
      }
    }
  }

  events.sort((a, b) => a.when.localeCompare(b.when));

  if (events.length === 0) {
    return <div class="home-coming-empty">No notable events in the next {horizonHours} h.</div>;
  }

  return (
    <ol class="coming-timeline">
      {events.map((e) => {
        const dt = Math.max(0, Date.parse(e.when) - nowMs);
        const pct = Math.max(0, Math.min(100, (1 - dt / (horizonMs - nowMs)) * 100));
        return (
          <li class="coming-timeline-item" key={`${e.kind}-${e.when}`}>
            <span class="coming-timeline-icon" style={{ background: e.color }}>{e.icon}</span>
            <div class="coming-timeline-body">
              <div class="coming-timeline-head">
                <span class="coming-timeline-title">{e.title}</span>
                <span class="coming-timeline-when">{hhmm(e.when)} · in {formatIn(dt)}</span>
              </div>
              <div class="coming-timeline-sub">{e.sub}</div>
              <div class="coming-timeline-bar" role="presentation">
                <div class="coming-timeline-bar-fill"
                     style={{ width: `${pct}%`, background: e.color }} />
              </div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

interface Slot { valid_from: string; p: number; }
function findRun(slots: Slot[], pred: (s: Slot) => boolean, onRun: (r: { first: Slot; count: number }) => void) {
  let start = -1, count = 0;
  for (let i = 0; i < slots.length; i++) {
    if (pred(slots[i])) {
      if (start < 0) start = i;
      count++;
    } else if (start >= 0) {
      onRun({ first: slots[start], count });
      return;
    }
  }
  if (start >= 0) onRun({ first: slots[start], count });
}

function formatIn(ms: number): string {
  const min = Math.round(ms / 60000);
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  const rem = min % 60;
  return rem === 0 ? `${h} h` : `${h}h ${rem}m`;
}
