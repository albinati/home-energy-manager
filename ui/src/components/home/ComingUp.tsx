import type { AgileTodayResponse, WeatherResponse } from "../../lib/types";
import { hhmm } from "../../lib/format";

interface ComingUpProps {
  agile: AgileTodayResponse | null;
  weather: WeatherResponse | null;
  cheapP: number;
  peakP: number;
  nowUtc: string;
}

type Event = {
  kind: "negative" | "cheap" | "peak" | "solar";
  when: string;     // ISO
  title: string;
  sub: string;
  color: string;
  icon: string;
};

// Timeline-styled list of the next few interesting things. Each event shows
// a progress bar marking time-to-event within the next 12h window so the
// user can see at a glance how soon something happens.
export function ComingUp({ agile, weather, cheapP, peakP, nowUtc }: ComingUpProps) {
  const nowMs = Date.parse(nowUtc);
  const horizonMs = 12 * 3600 * 1000;
  const events: Event[] = [];

  if (agile?.import_slots) {
    const future = agile.import_slots
      .slice()
      .sort((a, b) => a.valid_from.localeCompare(b.valid_from))
      .filter((s) => Date.parse(s.valid_from) > nowMs);

    const negStart = future.findIndex((s) => s.p < 0);
    if (negStart >= 0) {
      const first = future[negStart];
      let count = 0;
      for (let i = negStart; i < future.length && future[i].p < 0; i++) count++;
      events.push({
        kind: "negative", when: first.valid_from,
        title: "Negative price",
        sub: `${first.p.toFixed(1)}p · ${count * 30} min window`,
        color: "var(--neg-price)", icon: "🔵",
      });
    }
    const peakStart = future.findIndex((s) => s.p >= peakP);
    if (peakStart >= 0) {
      const first = future[peakStart];
      let count = 0;
      for (let i = peakStart; i < future.length && future[i].p >= peakP; i++) count++;
      events.push({
        kind: "peak", when: first.valid_from,
        title: "Peak price",
        sub: `${first.p.toFixed(1)}p · ${count * 30} min window`,
        color: "var(--peak)", icon: "🟠",
      });
    }
    let runStart = -1, runLen = 0;
    for (let i = 0; i < future.length; i++) {
      if (future[i].p < cheapP) {
        if (runStart < 0) runStart = i;
        runLen++;
        if (runLen >= 4) break;
      } else {
        runStart = -1;
        runLen = 0;
      }
    }
    if (runLen >= 4 && runStart >= 0) {
      const first = future[runStart];
      events.push({
        kind: "cheap", when: first.valid_from,
        title: "Cheap charging window",
        sub: `${first.p.toFixed(1)}p · ${runLen * 30}+ min`,
        color: "var(--cheap)", icon: "🟢",
      });
    }
  }

  if (weather?.forecast) {
    const future = weather.forecast.filter((f) => Date.parse(f.time) > nowMs);
    if (future.length > 0) {
      let peak = future[0];
      for (const f of future) if (f.pv_kw > peak.pv_kw) peak = f;
      if (peak.pv_kw > 0.5) {
        events.push({
          kind: "solar", when: peak.time,
          title: "Solar peak",
          sub: `${peak.pv_kw.toFixed(1)} kW forecast (${peak.temp_c?.toFixed(0) ?? "—"}°C)`,
          color: "var(--pv)", icon: "☀",
        });
      }
    }
  }

  events.sort((a, b) => a.when.localeCompare(b.when));

  if (events.length === 0) {
    return <div class="home-coming-empty">No notable price or solar events on the horizon.</div>;
  }

  return (
    <ol class="coming-timeline">
      {events.map((e) => {
        const dt = Math.max(0, Date.parse(e.when) - nowMs);
        const pct = Math.max(0, Math.min(100, (1 - dt / horizonMs) * 100));
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

function formatIn(ms: number): string {
  const min = Math.round(ms / 60000);
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  const rem = min % 60;
  return rem === 0 ? `${h} h` : `${h}h ${rem}m`;
}
