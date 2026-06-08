import type { WeatherResponse, PvTodayResponse } from "../../lib/types";
import "./weather.css";

interface Props {
  weather: WeatherResponse | null;
  pv?: PvTodayResponse | null;
}

// Apple/Tesla-style weather card tuned for a solar home: current condition +
// outdoor temp, today's solar generation curve (sunrise → sunset, peak, kWh
// expected), and an hourly strip with temperature, sky and PV potential. All
// inline SVG — no chart engine — so it stays light and crisp.
export function WeatherWidget({ weather, pv }: Props) {
  const fc = weather?.forecast ?? [];
  if (!fc.length) return <p class="muted">Weather unavailable.</p>;

  const nowMs = Date.now();
  // Current slot = the most recent forecast hour at or before now.
  let curIdx = 0;
  for (let i = 0; i < fc.length; i++) {
    if (new Date(fc[i].time).getTime() <= nowMs) curIdx = i; else break;
  }
  const cur = fc[curIdx];
  const outdoor = weather?.daikin?.outdoor_temp ?? cur.temp_c;
  const cloud = cur.cloud_cover_pct ?? null;
  const cond = condition(cloud);

  // Today's slots (local date) for the solar curve + hi/lo.
  const todayKey = new Date().toDateString();
  const today = fc.filter((s) => new Date(s.time).toDateString() === todayKey);
  const temps = today.map((s) => s.temp_c);
  const hi = temps.length ? Math.max(...temps) : null;
  const lo = temps.length ? Math.min(...temps) : null;
  // Solar still to come — expected generation from NOW to end of day (the
  // forward-looking number that's actionable), summing the /pv/today forecast
  // for the remaining slots. Falls back to the full-day total if slots absent.
  const pvNowMs = pv?.now_utc ? new Date(pv.now_utc).getTime() : nowMs;
  const solarRestOfDay = (pv?.slots ?? []).reduce((a, s) => {
    const start = new Date(s.slot_utc).getTime();
    return start >= pvNowMs ? a + Math.max(0, s.pv_forecast_kwh || 0) : a;
  }, 0);
  const pvDayTotal = pv?.forecast_kwh_day_total ?? null;
  const pvFromForecast = today.reduce((a, s) => a + Math.max(0, s.pv_kw || 0), 0);
  const hasSlots = (pv?.slots?.length ?? 0) > 0;
  const solarKwh = hasSlots ? solarRestOfDay : (pvDayTotal ?? pvFromForecast);

  const next = fc.slice(curIdx, curIdx + 12);

  return (
    <div class={`weather weather--${cond.tone}`}>
      <div class="wx-hero">
        <div class="wx-now">
          <span class="wx-temp">{Math.round(outdoor ?? 0)}<span class="wx-deg">°</span></span>
          <div class="wx-cond">
            <CondIcon kind={cond.icon} size={22} />
            <span>{cond.label}</span>
          </div>
          <div class="wx-hilo">
            {hi != null && lo != null && <>H {Math.round(hi)}° · L {Math.round(lo)}°</>}
            {cloud != null && <span class="wx-cloud"> · {Math.round(cloud)}% cloud</span>}
          </div>
        </div>
        <div class="wx-solar-sum">
          <SunIcon size={26} />
          <span class="wx-solar-kwh">{solarKwh.toFixed(1)}<span class="wx-solar-unit"> kWh</span></span>
          <span class="wx-solar-label">{hasSlots ? "solar expected · rest of day" : "solar expected today"}</span>
        </div>
      </div>

      <SolarCurve today={today} nowMs={nowMs} />

      <div class="wx-hours">
        {next.map((s, i) => {
          const c = condition(s.cloud_cover_pct ?? null);
          const h = new Date(s.time);
          const pvH = Math.max(0, s.pv_kw || 0);
          return (
            <div class="wx-hour" key={s.time}>
              <span class="wx-hour-t">{i === 0 ? "Now" : h.toLocaleTimeString([], { hour: "2-digit", hour12: false })}</span>
              <CondIcon kind={c.icon} size={18} />
              <span class="wx-hour-temp">{Math.round(s.temp_c)}°</span>
              <span class="wx-hour-pvbar" title={`${pvH.toFixed(1)} kW solar`}>
                <span class="wx-hour-pvfill" style={`height:${Math.round(Math.min(1, pvH / 4.5) * 100)}%`} />
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// --- condition mapping (cloud cover → label + icon + colour tone) ----------
function condition(cloud: number | null): { label: string; icon: IconKind; tone: string } {
  if (cloud == null) return { label: "—", icon: "sun", tone: "clear" };
  if (cloud < 15) return { label: "Clear", icon: "sun", tone: "clear" };
  if (cloud < 45) return { label: "Mostly sunny", icon: "sun-cloud", tone: "clear" };
  if (cloud < 70) return { label: "Partly cloudy", icon: "sun-cloud", tone: "partly" };
  if (cloud < 90) return { label: "Cloudy", icon: "cloud", tone: "cloudy" };
  return { label: "Overcast", icon: "cloud", tone: "overcast" };
}

// --- solar curve (inline SVG area sparkline) -------------------------------
function SolarCurve({ today, nowMs }: { today: { time: string; pv_kw: number }[]; nowMs: number }) {
  const pts = today.map((s) => Math.max(0, s.pv_kw || 0));
  const max = Math.max(1, ...pts);
  const W = 300, H = 54, pad = 2;
  if (today.length < 2) return null;
  const x = (i: number) => pad + (i / (today.length - 1)) * (W - 2 * pad);
  const y = (v: number) => H - pad - (v / max) * (H - 2 * pad);
  const line = today.map((_, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(pts[i]).toFixed(1)}`).join(" ");
  const area = `${line} L${x(today.length - 1).toFixed(1)},${H} L${x(0).toFixed(1)},${H} Z`;
  // peak marker + now marker
  let peakI = 0;
  for (let i = 1; i < pts.length; i++) if (pts[i] > pts[peakI]) peakI = i;
  let nowI = -1;
  for (let i = 0; i < today.length; i++) if (new Date(today[i].time).getTime() <= nowMs) nowI = i;

  return (
    <svg class="wx-solar-curve" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <linearGradient id="wxSolarFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="var(--pv, #fbbf24)" stop-opacity="0.45" />
          <stop offset="100%" stop-color="var(--pv, #fbbf24)" stop-opacity="0.02" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#wxSolarFill)" />
      <path d={line} fill="none" stroke="var(--pv, #fbbf24)" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke" />
      {pts[peakI] > 0.2 && <circle cx={x(peakI)} cy={y(pts[peakI])} r="2.4" fill="var(--pv, #fbbf24)" />}
      {nowI >= 0 && <line x1={x(nowI)} y1="0" x2={x(nowI)} y2={H} stroke="var(--text)" stroke-width="1" stroke-opacity="0.35" vector-effect="non-scaling-stroke" />}
    </svg>
  );
}

// --- tiny weather glyphs ---------------------------------------------------
type IconKind = "sun" | "sun-cloud" | "cloud";
function CondIcon({ kind, size }: { kind: IconKind; size: number }) {
  if (kind === "sun") return <SunIcon size={size} />;
  if (kind === "cloud") return <CloudIcon size={size} />;
  return <SunCloudIcon size={size} />;
}
function SunIcon({ size }: { size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="var(--pv, #fbbf24)" stroke-width="2" stroke-linecap="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4.5" fill="var(--pv, #fbbf24)" stroke="none" />
      <g stroke="var(--pv, #fbbf24)"><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M19.1 4.9l-1.8 1.8M6.7 17.3l-1.8 1.8" /></g>
    </svg>
  );
}
function CloudIcon({ size }: { size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M7 18h10a4 4 0 0 0 .5-7.97A6 6 0 0 0 6 9.5 3.5 3.5 0 0 0 7 18z" fill="var(--text-mute, #9ca3af)" fill-opacity="0.85" />
    </svg>
  );
}
function SunCloudIcon({ size }: { size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="8" cy="8" r="3.5" fill="var(--pv, #fbbf24)" />
      <g stroke="var(--pv, #fbbf24)" stroke-width="1.6" stroke-linecap="round"><path d="M8 1.5V3M1.5 8H3M3.4 3.4l1 1M12.6 3.4l-1 1M8 12.5V14" /></g>
      <path d="M9 19h8a3.3 3.3 0 0 0 .4-6.58A5 5 0 0 0 8 11.8 3 3 0 0 0 9 19z" fill="var(--text-mute, #9ca3af)" fill-opacity="0.9" />
    </svg>
  );
}
