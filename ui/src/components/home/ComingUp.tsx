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
  when: string;
  title: string;
  sub: string;
  color: string;
  icon: string;
};

export function ComingUp({ agile, weather, cheapP, peakP, nowUtc }: ComingUpProps) {
  const nowMs = Date.parse(nowUtc);
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
        kind: "negative",
        when: first.valid_from,
        title: "Negative price",
        sub: `${first.p.toFixed(1)}p · ${count * 30} min window`,
        color: "var(--neg-price)",
        icon: "🔵",
      });
    }

    const peakStart = future.findIndex((s) => s.p >= peakP);
    if (peakStart >= 0) {
      const first = future[peakStart];
      let count = 0;
      for (let i = peakStart; i < future.length && future[i].p >= peakP; i++) count++;
      events.push({
        kind: "peak",
        when: first.valid_from,
        title: "Peak price",
        sub: `${first.p.toFixed(1)}p · ${count * 30} min window`,
        color: "var(--peak)",
        icon: "🟠",
      });
    }

    let runStart = -1;
    let runLen = 0;
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
        kind: "cheap",
        when: first.valid_from,
        title: "Cheap charging window",
        sub: `${first.p.toFixed(1)}p · ${runLen * 30}+ min`,
        color: "var(--cheap)",
        icon: "🟢",
      });
    }
  }

  if (weather?.forecast) {
    const future = weather.forecast.filter((f) => Date.parse(f.time) > nowMs);
    if (future.length > 0) {
      let peak = future[0];
      for (const f of future) {
        if (f.pv_kw > peak.pv_kw) peak = f;
      }
      if (peak.pv_kw > 0.5) {
        events.push({
          kind: "solar",
          when: peak.time,
          title: "Solar peak",
          sub: `${peak.pv_kw.toFixed(1)} kW forecast (${peak.temp_c?.toFixed(0) ?? "—"}°C)`,
          color: "var(--pv)",
          icon: "☀",
        });
      }
    }
  }

  events.sort((a, b) => a.when.localeCompare(b.when));

  if (events.length === 0) {
    return <div class="home-coming-empty">No notable price or solar events on the horizon.</div>;
  }

  return (
    <div class="home-coming">
      {events.map((e) => (
        <div
          key={`${e.kind}-${e.when}`}
          class="home-coming-item"
          style={{ borderLeftColor: e.color }}
        >
          <span class="home-coming-icon" style={{ background: e.color }}>{e.icon}</span>
          <div class="home-coming-body">
            <div class="home-coming-title">{e.title}</div>
            <div class="home-coming-sub">{e.sub}</div>
          </div>
          <div class="home-coming-when">{hhmm(e.when)}</div>
        </div>
      ))}
    </div>
  );
}
