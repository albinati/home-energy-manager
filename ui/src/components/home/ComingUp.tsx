import type { AgileDayResponse, WeatherResponse } from "../../lib/types";
import { hhmm, pence } from "../../lib/format";

interface ComingUpProps {
  agile: AgileDayResponse | null;
  weather: WeatherResponse | null;
  cheapP: number;
  peakP: number;
  nowUtc: string;
}

type Event = {
  kind: "negative" | "cheap" | "peak" | "solar";
  when: string;            // ISO
  title: string;
  sub: string;
  color: string;
  icon: string;
};

// Surfaces the next few interesting things on today's horizon: next negative
// price slot, next peak window, today's forecast solar peak. Empty when
// today's plan is uneventful — useful in itself.
export function ComingUp({ agile, weather, cheapP, peakP, nowUtc }: ComingUpProps) {
  const nowMs = Date.parse(nowUtc);
  const events: Event[] = [];

  // --- Next negative-price window ---
  if (agile?.import) {
    const future = agile.import
      .slice()
      .sort((a, b) => a.slot_time_utc.localeCompare(b.slot_time_utc))
      .filter((s) => Date.parse(s.slot_time_utc) > nowMs);
    const negStart = future.findIndex((s) => s.value_inc_vat < 0);
    if (negStart >= 0) {
      const first = future[negStart];
      let count = 0;
      for (let i = negStart; i < future.length && future[i].value_inc_vat < 0; i++) count++;
      events.push({
        kind: "negative",
        when: first.slot_time_utc,
        title: "Negative price",
        sub: `${first.value_inc_vat.toFixed(1)}p · ${count * 30} min window`,
        color: "var(--neg-price)",
        icon: "🔵",
      });
    }
    // Next peak window (>= peakP)
    const peakStart = future.findIndex((s) => s.value_inc_vat >= peakP);
    if (peakStart >= 0) {
      const first = future[peakStart];
      let count = 0;
      for (let i = peakStart; i < future.length && future[i].value_inc_vat >= peakP; i++) count++;
      events.push({
        kind: "peak",
        when: first.slot_time_utc,
        title: "Peak price",
        sub: `${first.value_inc_vat.toFixed(1)}p · ${count * 30} min window`,
        color: "var(--peak)",
        icon: "🟠",
      });
    }
    // Next cheap-cluster (≥ 4 contiguous slots below cheap threshold)
    let runStart = -1;
    let runLen = 0;
    for (let i = 0; i < future.length; i++) {
      if (future[i].value_inc_vat < cheapP) {
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
        when: first.slot_time_utc,
        title: "Cheap charging window",
        sub: `${first.value_inc_vat.toFixed(1)}p · ${runLen * 30}+ min`,
        color: "var(--cheap)",
        icon: "🟢",
      });
    }
  }

  // --- Today's solar peak (max pv_kw in the forecast after now) ---
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

  // Sort by time
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

// Re-export pence for callers that want to render thresholds nicely.
export { pence };
