import type { WeatherResponse, WeatherSlot } from "../../lib/types";
import "./forecast-strip.css";

// Next-3-days weather + solar outlook, rendered inline inside the hero weather
// panel (today is already the "now" block above, so today is skipped).
// Aggregates the hourly /weather forecast (96 h) into per-day buckets: hi/lo
// temp, a condition (rain from the WMO weather_code / precipitation, else cloud
// cover), and the estimated PV kWh for the day. SVG glyphs only (no emoji).
type Cond = "sun" | "sun-cloud" | "cloud" | "rain";

interface DayAgg {
  key: string;
  label: string;
  isToday: boolean;
  hi: number;
  lo: number;
  pvKwh: number;
  cond: Cond;
  condLabel: string;
}

// WMO weather code (+ precip / cloud fallback) → glyph + label. Snow/fog fold
// onto the cloud glyph (rare here) with an honest label; rain covers drizzle →
// showers → thunderstorm.
function condFor(code: number, precipMm: number, cloud: number | null): { cond: Cond; label: string } {
  if (code >= 95) return { cond: "rain", label: "Storm" };
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return { cond: "cloud", label: "Snow" };
  if (code >= 51 || precipMm >= 0.5) return { cond: "rain", label: "Rain" };
  if (code === 45 || code === 48) return { cond: "cloud", label: "Fog" };
  if (cloud == null || cloud < 25) return { cond: "sun", label: "Clear" };
  if (cloud < 60) return { cond: "sun-cloud", label: "Partly cloudy" };
  if (cloud < 85) return { cond: "cloud", label: "Cloudy" };
  return { cond: "cloud", label: "Overcast" };
}

export function ForecastStrip({ weather }: { weather?: WeatherResponse | null }) {
  const fc = weather?.forecast ?? [];
  if (fc.length < 24) return null; // need at least ~a day to be useful

  const byDay = new Map<string, WeatherSlot[]>();
  for (const s of fc) {
    const key = new Date(s.time).toDateString();
    let arr = byDay.get(key);
    if (!arr) { arr = []; byDay.set(key, arr); }
    arr.push(s);
  }

  const todayKey = new Date().toDateString();
  const days: DayAgg[] = [];
  for (const [key, slots] of byDay) {
    const temps = slots.map((s) => s.temp_c);
    const hi = Math.max(...temps);
    const lo = Math.min(...temps);
    const pvKwh = slots.reduce((a, s) => a + Math.max(0, s.pv_kw || 0), 0);
    const precipTotal = slots.reduce((a, s) => a + Math.max(0, s.precipitation_mm || 0), 0);
    // Daytime hours drive the condition (a clear night shouldn't read "Clear day").
    const dayHrs = slots.filter((s) => { const h = new Date(s.time).getHours(); return h >= 8 && h <= 18; });
    const ref = dayHrs.length ? dayHrs : slots;
    const worstCode = ref.reduce((m, s) => Math.max(m, s.weather_code ?? 0), 0);
    const meanCloud = ref.length ? ref.reduce((a, s) => a + (s.cloud_cover_pct ?? 0), 0) / ref.length : null;
    const { cond, label } = condFor(worstCode, precipTotal, meanCloud);
    const isToday = key === todayKey;
    days.push({
      key,
      label: isToday ? "Today" : new Date(key).toLocaleDateString([], { weekday: "short" }),
      isToday,
      hi, lo, pvKwh, cond, condLabel: label,
    });
  }

  // Today is the "now" block in the hero — show the next 3 days only.
  const shown = days.filter((d) => !d.isToday).slice(0, 3);
  if (!shown.length) return null;

  return (
    <div class="fcstrip" role="group" aria-label="Next 3 days weather and solar forecast"
         style={{ gridTemplateColumns: `repeat(${shown.length}, 1fr)` }}>
      {shown.map((d) => (
        <div class="fcstrip-day" key={d.key}>
          <span class="fcstrip-label">{d.label}</span>
          <CondGlyph cond={d.cond} />
          <span class="fcstrip-cond">{d.condLabel}</span>
          <span class="fcstrip-temp"><b>{Math.round(d.hi)}°</b> <span class="fcstrip-lo">{Math.round(d.lo)}°</span></span>
          <span class="fcstrip-pv">{d.pvKwh.toFixed(1)}<span class="fcstrip-pv-unit"> kWh PV</span></span>
        </div>
      ))}
    </div>
  );
}

function CondGlyph({ cond }: { cond: Cond }) {
  const pv = "var(--pv, #fbbf24)";
  const mute = "var(--text-mute, #9ca3af)";
  const rain = "var(--grid, #60a5fa)";
  if (cond === "sun") {
    return (
      <svg class="fcstrip-icon" width="24" height="24" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="4.5" fill={pv} />
        <g stroke={pv} stroke-width="2" stroke-linecap="round">
          <path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M19.1 4.9l-1.8 1.8M6.7 17.3l-1.8 1.8" />
        </g>
      </svg>
    );
  }
  if (cond === "sun-cloud") {
    return (
      <svg class="fcstrip-icon" width="24" height="24" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="8" cy="8" r="3.5" fill={pv} />
        <g stroke={pv} stroke-width="1.6" stroke-linecap="round"><path d="M8 1.5V3M1.5 8H3M3.4 3.4l1 1M12.6 3.4l-1 1" /></g>
        <path d="M9 19h8a3.3 3.3 0 0 0 .4-6.58A5 5 0 0 0 8 11.8 3 3 0 0 0 9 19z" fill={mute} fill-opacity="0.9" />
      </svg>
    );
  }
  if (cond === "rain") {
    return (
      <svg class="fcstrip-icon" width="24" height="24" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M7 15h10a4 4 0 0 0 .5-7.97A6 6 0 0 0 6 6.5 3.5 3.5 0 0 0 7 15z" fill={mute} fill-opacity="0.85" />
        <g stroke={rain} stroke-width="1.8" stroke-linecap="round"><path d="M8 18l-1 2.5M12 18l-1 2.5M16 18l-1 2.5" /></g>
      </svg>
    );
  }
  return (
    <svg class="fcstrip-icon" width="24" height="24" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M7 18h10a4 4 0 0 0 .5-7.97A6 6 0 0 0 6 9.5 3.5 3.5 0 0 0 7 18z" fill={mute} fill-opacity="0.85" />
    </svg>
  );
}
