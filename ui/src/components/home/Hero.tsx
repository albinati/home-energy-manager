import type {
  MetricsResponse, CockpitNow, AgileTodayResponse,
  PeriodInsightsResponse, TodayCumulativeResponse, WeatherResponse, PvTodayResponse,
} from "../../lib/types";
import { gbp, kwh } from "../../lib/format";
import { useAnimatedNumber } from "../../lib/useAnimatedNumber";
import { isCurrentPeriod, periodLabel, type PeriodState } from "../../lib/period";
import { Icon } from "../common/Icon";
import { Link } from "wouter-preact";
import "./hero.css";

interface HeroProps {
  metrics: MetricsResponse | null;
  metricsLoading: boolean;
  cockpit: CockpitNow | null;
  agile: AgileTodayResponse | null;
  period: PeriodInsightsResponse | null;
  periodState: PeriodState;
  periodLoading: boolean;
  todayCum?: TodayCumulativeResponse | null;
  weather?: WeatherResponse | null;
  pv?: PvTodayResponse | null;
}

// The redesign hero (Claude Design handoff): the period's net bill + an
// Agile-vs-fixed verdict + cost bars on the LEFT, the live weather panel on
// the RIGHT. Money figures follow the period navigator; the today-only extras
// (break-even target, money paid in) show only on "today". The lifetime strip
// moved to the foot of the cockpit (LifetimeStrip) — the hero is today-first.
export function Hero({ metrics, period, periodState, periodLoading, todayCum, weather, pv }: HeroProps) {
  const isNow = isCurrentPeriod(periodState);
  const label = periodLabel(periodState);
  const fixedLabel = todayCum?.fixed_tariff_label || metrics?.fixed_tariff?.label || "British Gas Fixed";

  // --- Money story for the SELECTED period (period-aware; today == todayCum). ---
  // On the CURRENT period, fall back to todayCum's running net bill while the
  // (slower) period aggregate is still loading or failed — the £ headline is
  // the first thing the eye looks for and must never sit on a skeleton when a
  // 60s-polled number already knows the answer (mobile showed exactly that).
  // Day-granularity ONLY: todayCum is a single day's figure — labelling it
  // as the current week/month/year while the aggregate loads (or after it
  // fails) would put a mislabelled £ on a money display (review M on #552).
  const isTodayView = periodState.gran === "day" && isNow;
  const bill = period?.cost?.net_cost_pounds
    ?? (isTodayView ? todayCum?.realised_net_cost_gbp ?? null : null);
  const savedVsBG = period?.cost?.delta_vs_fixed_real_pounds
    ?? (isNow ? todayCum?.delta_vs_fixed_tariff_real_gbp : null) ?? null;
  const grid = period?.energy?.import_kwh ?? (isNow ? todayCum?.import_kwh : null) ?? null;
  const standing = period?.cost?.standing_charge_pence != null
    ? period.cost.standing_charge_pence / 100
    : (isNow ? todayCum?.standing_charge_gbp ?? null : null);
  const fixedShadow = bill != null && savedVsBG != null ? bill + savedVsBG : null;
  const win = (savedVsBG ?? 0) >= 0;

  // --- Today-only extras (the user's earlier asks, kept as quiet notes). ---
  const breakevenP = isNow ? todayCum?.breakeven_avg_import_p ?? null : null;
  const realisedAvgP = isNow ? todayCum?.realised_avg_import_p ?? null : null;
  const beatingTarget = breakevenP != null && realisedAvgP != null && realisedAvgP <= breakevenP;
  const earnings = isNow ? todayCum?.earnings_today_gbp ?? null : null;
  const negCredit = isNow ? todayCum?.negative_import_credit_gbp ?? null : null;
  const exportRev = isNow ? todayCum?.export_revenue_gbp ?? null : null;
  const showEarnings = (earnings ?? 0) > 0.005;

  const billA = useAnimatedNumber(bill);
  const max = Math.max(bill ?? 0, fixedShadow ?? 0) * 1.12 || 1;

  return (
    <section class="hero" aria-label="Selected period energy outcome">
      <div class="hero-grid">
        {/* ── LEFT: the money story ─────────────────────────────────── */}
        <div class="hero-left">
          <div class="eyebrow"><Icon name="cost" size={13} />{label} on Agile · net bill{isNow ? " so far" : ""}</div>
          <div class="hero-number">
            {billA == null ? (periodLoading ? <span class="skel-text" style={{ width: "7rem", height: "0.8em" }} /> : "—") : gbp(billA)}
          </div>

          {savedVsBG != null && (
            <div class="verdict-wrap">
              <span class={`verdict ${win ? "verdict--win" : "verdict--behind"}`}>
                <Icon name={win ? "check" : "trend"} size={15} />
                {win ? "Beating fixed by " : "Behind fixed by "}{gbp(Math.abs(savedVsBG))}
                <span class="verdict-sub">· vs {fixedLabel}</span>
              </span>
              <Link href="/insights" class="league-note">
                Compare every tariff<span class="ln-cta"><Icon name="chevron" size={12} /></span>
              </Link>
            </div>
          )}

          {grid != null && (
            <div class="statline">
              <div class="stat">
                <div class="stat-v">{kwh(grid, 1)}</div>
                <div class="stat-l">Grid import (billed)</div>
              </div>
            </div>
          )}

          {/* cost-so-far vs fixed — the two bars */}
          {bill != null && fixedShadow != null && (
            <div class="vsfixed">
              <div class="flex between items-baseline vsfixed-head">
                <span class="stat-l">Cost vs fixed tariff</span>
                <span class="rate-sub">measured grid use</span>
              </div>
              <div class="vf-bars">
                <div class="vf-row">
                  <span class="vf-k">Agile</span>
                  <div class="vf-track"><div class="vf-fill" style={{ width: `${Math.min(100, bill / max * 100)}%`, background: win ? "var(--ok)" : "var(--bad)" }} /></div>
                  <span class="vf-v">{gbp(bill)}</span>
                </div>
                <div class="vf-row">
                  <span class="vf-k">Fixed</span>
                  <div class="vf-track"><div class="vf-fill" style={{ width: `${Math.min(100, fixedShadow / max * 100)}%`, background: "var(--text-mute)" }} /></div>
                  <span class="vf-v">{gbp(fixedShadow)}</span>
                </div>
              </div>
            </div>
          )}

          {standing != null && standing > 0.0001 && (
            <div class="standing-note"><Icon name="cost" size={12} />Standing charge {gbp(standing)}/day · fixed, applies either way</div>
          )}

          {/* today-only quiet notes (kept from earlier asks) */}
          {breakevenP != null && (
            <div class="hero-note" title={`To beat ${fixedLabel}, keep the average import price ≤ ${breakevenP.toFixed(1)}p/kWh — Agile's higher standing must be won back on the unit rate.`}>
              <span class="hero-note-pair">Target: import avg ≤ <strong>{breakevenP.toFixed(1)}p</strong></span>
              {realisedAvgP != null && (
                <span class="hero-note-pair"> · now{" "}
                  <strong class={beatingTarget ? "pos" : "neg"}>
                    {realisedAvgP.toFixed(1)}p <Icon name={beatingTarget ? "check" : "cross"} size={11} />
                  </strong>
                </span>
              )}
            </div>
          )}
          {showEarnings && earnings != null && (
            <div class="hero-note">
              Paid in <strong class="pos">{gbp(earnings)}</strong>
              {(negCredit ?? 0) > 0.005 && (exportRev ?? 0) > 0.005
                ? <> ({gbp(negCredit!)} negative + {gbp(exportRev!)} export)</>
                : (negCredit ?? 0) > 0.005 ? <> (negative import)</> : <> (export)</>}
            </div>
          )}
        </div>

        {/* ── RIGHT: live weather ───────────────────────────────────── */}
        <div class="hero-right"><HeroWeather weather={weather} pv={pv} /></div>
      </div>
    </section>
  );
}

/* ── Weather panel (hero-right) ──────────────────────────────────────── */
type Cond = "clear" | "partly" | "cloud" | "rain";
function condOf(cloud: number | null | undefined): Cond {
  if (cloud == null) return "partly";
  if (cloud < 25) return "clear";
  if (cloud < 60) return "partly";
  return "cloud";
}
function condLabel(c: Cond): string {
  return c === "clear" ? "Clear" : c === "partly" ? "Partly cloudy" : c === "rain" ? "Rain" : "Cloudy";
}

function HeroWeather({ weather, pv }: { weather?: WeatherResponse | null; pv?: PvTodayResponse | null }) {
  const fc = weather?.forecast ?? [];
  if (!fc.length) return <div class="hw"><span class="muted">Weather unavailable.</span></div>;
  const nowMs = Date.now();
  let curIdx = 0;
  for (let i = 0; i < fc.length; i++) { if (new Date(fc[i].time).getTime() <= nowMs) curIdx = i; else break; }
  const cur = fc[curIdx];
  const outdoor = weather?.daikin?.outdoor_temp ?? cur.temp_c;
  const cond = condOf(cur.cloud_cover_pct);

  // Today's hi/lo + a now-marker on the range.
  const todayKey = new Date().toDateString();
  const todaySlots = fc.filter((s) => new Date(s.time).toDateString() === todayKey);
  const temps = todaySlots.map((s) => s.temp_c);
  const hi = temps.length ? Math.max(...temps) : null;
  const lo = temps.length ? Math.min(...temps) : null;
  const markPct = hi != null && lo != null && hi > lo
    ? Math.max(6, Math.min(94, ((outdoor - lo) / (hi - lo)) * 100)) : 50;

  // Solar today: generated so far (actual) toward the DAY total (locked actuals
  // for elapsed slots + forecast for the rest). Using the day total — not the
  // forward-only forecast — so it stays meaningful in the evening (forecast → 0).
  const pvNowMs = pv?.now_utc ? new Date(pv.now_utc).getTime() : nowMs;
  const slots = pv?.slots ?? [];
  const elapsedOf = (s: { slot_utc: string }) => new Date(s.slot_utc).getTime() + 30 * 60_000 <= pvNowMs;
  const solarDone = slots.reduce((a, s) => a + (s.pv_actual_kwh ?? 0), 0);
  const solarTotal = slots.length
    ? slots.reduce((a, s) => a + ((elapsedOf(s) ? (s.pv_actual_kwh ?? s.pv_forecast_kwh) : s.pv_forecast_kwh) ?? 0), 0)
    : (pv?.forecast_kwh_day_total ?? 0);
  const solarToGo = slots.reduce((a, s) => (!elapsedOf(s) ? a + (s.pv_forecast_kwh ?? 0) : a), 0);
  const solarPct = solarTotal > 0 ? Math.min(100, Math.round((solarDone / solarTotal) * 100)) : 0;
  // Peak slot (max actual-or-forecast) → local HH:MM.
  let peakV = -1, peakIso = "";
  for (const s of slots) { const v = Math.max(s.pv_actual_kwh ?? 0, s.pv_forecast_kwh ?? 0); if (v > peakV) { peakV = v; peakIso = s.slot_utc; } }
  const peakLabel = peakV > 0.05 && peakIso ? new Date(peakIso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : null;

  return (
    <div class="hw">
      <div class="flex between items-center">
        <div class="eyebrow weather-eyebrow"><WxIcon cond={cond} size={13} />Weather · now</div>
      </div>
      <div class="hw-now">
        <div class="weather-temp">{Math.round(outdoor)}°</div>
        <div class="hw-cond">
          <WxIcon cond={cond} size={26} />
          <div>
            <div class="hw-cond-label">{condLabel(cond)}</div>
            {cur.cloud_cover_pct != null && <div class="dim small">{Math.round(cur.cloud_cover_pct)}% cloud</div>}
          </div>
        </div>
      </div>

      {hi != null && lo != null && (
        <div class="thermo">
          <div class="thermo-track"><span class="thermo-mark" style={{ left: `${markPct}%` }} /></div>
          <div class="thermo-row">
            <span class="t-lo">L {Math.round(lo)}°</span>
            <span class="dim small">today's range</span>
            <span class="t-hi">H {Math.round(hi)}°</span>
          </div>
        </div>
      )}

      {solarTotal > 0 && (
        <div class="solar-prog">
          <div class="flex between items-baseline solar-prog-head">
            <span class="rate-k weather-eyebrow"><Icon name="solar" size={13} style={{ color: "var(--pv)" }} />Solar today</span>
            <span class="rate-sub"><b class="solar-done">{solarDone.toFixed(1)}</b> / {solarTotal.toFixed(1)} kWh forecast</span>
          </div>
          <div class="solar-prog-track"><div class="solar-prog-fill" style={{ width: `${solarPct}%` }} /></div>
          <div class="thermo-row">
            <span class="dim small">{solarPct}% generated</span>
            <span class="dim small">{solarToGo > 0.05 ? `${solarToGo.toFixed(1)} kWh to go` : "done for today"}{peakLabel ? ` · peak ${peakLabel}` : ""}</span>
          </div>
        </div>
      )}
    </div>
  );
}

// Thin-line weather glyphs (no emoji), sized to match the Icon family.
function WxIcon({ cond, size = 20 }: { cond: Cond; size?: number }) {
  const s = { width: size, height: size, display: "block" } as const;
  if (cond === "clear") return (
    <svg viewBox="0 0 24 24" style={s} fill="none" stroke="var(--pv)" stroke-width="1.75" stroke-linecap="round">
      <circle cx="12" cy="12" r="4" /><path d="M12 3 V5 M12 19 V21 M3 12 H5 M19 12 H21 M5.6 5.6 L7 7 M17 17 L18.4 18.4 M18.4 5.6 L17 7 M7 17 L5.6 18.4" />
    </svg>);
  if (cond === "rain") return (
    <svg viewBox="0 0 24 24" style={s} fill="none" stroke="var(--grid)" stroke-width="1.75" stroke-linecap="round">
      <path d="M7 16 H16.5 A3 3 0 0 0 16.5 10 A3.4 3.4 0 0 0 10 9.6 A3.2 3.2 0 0 0 7 16 Z" /><path d="M9 19 L8.5 21 M13 19 L12.5 21 M16 19 L15.5 21" />
    </svg>);
  if (cond === "cloud") return (
    <svg viewBox="0 0 24 24" style={s} fill="none" stroke="var(--text-dim)" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
      <path d="M7 18 H16.5 A3.2 3.2 0 0 0 16.5 11.6 A3.6 3.6 0 0 0 9.5 11 A3.3 3.3 0 0 0 7 18 Z" />
    </svg>);
  // partly
  return (
    <svg viewBox="0 0 24 24" style={s} fill="none" stroke="var(--pv)" stroke-width="1.75" stroke-linecap="round">
      <circle cx="9" cy="8" r="3" /><path d="M9 3.2 V4.4 M4.2 8 H5.4 M5.6 4.6 L6.4 5.4 M12.4 4.6 L11.6 5.4" />
      <path d="M8 19 H16.5 A3 3 0 0 0 16.5 13 A3.4 3.4 0 0 0 10 12.6 A3.2 3.2 0 0 0 8 19 Z" stroke="var(--text-dim)" />
    </svg>);
}
