import type {
  MetricsResponse, CockpitNow, AgileTodayResponse,
  PeriodInsightsResponse, TodayCumulativeResponse, WeatherResponse, PvTodayResponse,
  IndoorSummary,
} from "../../lib/types";
import { gbp, kwh } from "../../lib/format";
import { useAnimatedNumber } from "../../lib/useAnimatedNumber";
import { isCurrentPeriod, periodLabel, type PeriodState } from "../../lib/period";
import { Icon } from "../common/Icon";
import { ForecastStrip } from "./ForecastStrip";
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
export function Hero({ metrics, cockpit, period, periodState, periodLoading, todayCum, weather, pv }: HeroProps) {
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
        <div class="hero-right"><HeroWeather weather={weather} pv={pv} indoor={cockpit?.state?.indoor ?? null} /></div>
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

function HeroWeather({ weather, pv, indoor }: {
  weather?: WeatherResponse | null; pv?: PvTodayResponse | null; indoor?: IndoorSummary | null;
}) {
  const fc = weather?.forecast ?? [];
  if (!fc.length) return <div class="hw"><span class="muted">Weather unavailable.</span></div>;
  const nowMs = Date.now();
  let curIdx = 0;
  for (let i = 0; i < fc.length; i++) { if (new Date(fc[i].time).getTime() <= nowMs) curIdx = i; else break; }
  const cur = fc[curIdx];
  // Outdoor REAL (Daikin sensor) vs ESTIMATED (Open-Meteo forecast at now). The
  // hero shows the measured value big + the forecast as a "vs est" comparison;
  // when there's no sensor the forecast IS the number (no separate est line).
  const outdoorReal = weather?.daikin?.outdoor_temp ?? null;
  const outdoorEst = cur.temp_c ?? null;
  const outdoor = outdoorReal ?? outdoorEst ?? 0;
  const showEst = outdoorReal != null && outdoorEst != null;
  const cond = condOf(cur.cloud_cover_pct);

  // Indoor (house room sensors) — the peer of Outside in the two-column head.
  const inRooms = indoor?.rooms ?? [];
  const inWithTemp = inRooms.filter((r) => r.temp_c != null);
  const indoorMean = indoor?.mean_c ?? (inWithTemp.length
    ? inWithTemp.reduce((s, r) => s + (r.temp_c as number), 0) / inWithTemp.length : null);
  const indoorStale = !!indoor?.stale;
  // Per-room mini-cards instead of a bare "N rooms": the mean is honest but hides
  // an outlier sensor (a hallway probe self-heating to 39° next to a 30° kitchen
  // pulls the mean to 35° and looks like a bug). One card per room makes the
  // spread — and a misplaced/faulty sensor — obvious at a glance. Sorted by
  // name so the cards hold still across polls (API order isn't stable).
  const roomCards = inWithTemp
    .map((r) => ({
      room: (r.room ?? "inside").replace(/_/g, " "),
      temp: r.temp_c as number,
      hum: r.humidity_pct,
      stale: r.stale,
    }))
    .sort((a, b) => a.room.localeCompare(b.room));
  // Rooms exist but none carries a temperature right now (humidity-only or all
  // null) — fall back to the old count label rather than an empty row.
  const roomFallback = (indoor?.n_rooms ?? 0) === 1
    ? (inRooms[0]?.room ?? "inside").replace(/_/g, " ")
    : `${indoor?.n_rooms} rooms`;
  const indoorHum = indoor?.humidity_pct ?? null;
  const indoorIso = indoor?.newest_received_at ?? indoor?.newest_captured_at ?? null;
  const indoorRefresh = indoorIso
    ? new Date(indoorIso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })
    : null;
  const hasIndoor = (indoor?.n_rooms ?? 0) > 0;

  // Hi/lo over a rolling next-24h window + the current reading. "Today's
  // remaining hours" collapses to a flat range in the evening (e.g. L19 H19)
  // and the live outdoor temp can sit outside it — including the current
  // reading and a full 24h of forecast keeps the range real and the now-marker
  // always in bounds.
  const next24 = fc.slice(curIdx, curIdx + 24).map((s) => s.temp_c);
  const rangeTemps = [outdoor, ...next24].filter((t) => Number.isFinite(t));
  const hi = rangeTemps.length ? Math.max(...rangeTemps) : null;
  const lo = rangeTemps.length ? Math.min(...rangeTemps) : null;
  const markPct = hi != null && lo != null && hi > lo
    ? Math.max(6, Math.min(94, ((outdoor - lo) / (hi - lo)) * 100)) : 50;

  // Solar today: generated so far (actual) vs the day-ahead FORECAST. The total
  // is the COMMITTED full-day forecast (pv_planned_kwh, frozen at solve time) —
  // not a blend of actual(past)+live-forward-forecast(future), which collapses
  // to the actual total in the evening (showing a meaningless 18.3/18.3, always
  // 100%) and hides the real forecast. pv_planned is full-day so it stays
  // meaningful all evening; fall back to the live forecast where uncommitted.
  const pvNowMs = pv?.now_utc ? new Date(pv.now_utc).getTime() : nowMs;
  const slots = pv?.slots ?? [];
  const elapsedOf = (s: { slot_utc: string }) => new Date(s.slot_utc).getTime() + 30 * 60_000 <= pvNowMs;
  const solarDone = slots.reduce((a, s) => a + (s.pv_actual_kwh ?? 0), 0);
  const solarTotal = slots.length
    ? slots.reduce((a, s) => a + ((s.pv_planned_kwh ?? s.pv_forecast_kwh) ?? 0), 0)
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
        <div class="eyebrow weather-eyebrow"><WxIcon cond={cond} size={13} />Climate · now</div>
        {hasIndoor && indoorRefresh && (
          <span class="dim small hw-refresh" title="Last indoor sensor reading">
            <span class={`hw-indoor-dot ${indoorStale ? "is-stale" : "live-pulse"}`} aria-hidden="true" />{indoorRefresh}
          </span>
        )}
      </div>

      {/* Outside vs Inside — peers. Outside = measured (big) vs forecast (est);
          Inside = the house room sensors (#540 W1). */}
      <div class="hw-head">
        <div class="hw-col hw-col--out">
          <div class="eyebrow hw-col-label">Outdoor</div>
          <div class="hw-col-now">
            <span class="hw-col-temp">
              {Math.round(outdoor)}°
              {showEst && <sup class="hw-col-est" title="Open-Meteo forecast for now">est {Math.round(outdoorEst as number)}°</sup>}
            </span>
            <WxIcon cond={cond} size={22} />
          </div>
          <div class="dim small hw-col-sub">
            {condLabel(cond)}
            {cur.cloud_cover_pct != null && <> · {Math.round(cur.cloud_cover_pct)}%</>}
          </div>
        </div>

        <div class={`hw-col hw-col--in ${indoorStale ? "is-stale" : ""}`}>
          <div class="eyebrow hw-col-label">Indoor</div>
          {hasIndoor ? (
            <>
              <div class="hw-col-now">
                <span class="hw-col-temp">{indoorMean != null ? indoorMean.toFixed(1) : "—"}°</span>
                <span class="dim small hw-col-meta">
                  house avg{indoor?.n_rooms ? ` · ${indoor.n_rooms} ${indoor.n_rooms === 1 ? "room" : "rooms"}` : ""}
                </span>
              </div>
              {roomCards.length ? (
                <div class="hw-rooms">
                  {roomCards.map((c) => (
                    <div key={c.room} class={`hw-room-card ${c.stale ? "is-stale" : ""}`}>
                      <div class="hw-room-card-head">
                        <span class="hw-room-card-name" title={c.room}>{c.room}</span>
                        <span class={`hw-room-dot ${c.stale ? "is-stale" : "live-pulse"}`} aria-hidden="true" />
                      </div>
                      <span class="hw-room-card-temp">{c.temp.toFixed(1)}°</span>
                      {c.hum != null && <span class="dim hw-room-card-hum">{Math.round(c.hum)}% RH</span>}
                    </div>
                  ))}
                </div>
              ) : (
                <div class="dim small hw-col-sub hw-col-sub--empty">{roomFallback}{indoorHum != null ? ` · ${Math.round(indoorHum)}% RH` : ""}</div>
              )}
            </>
          ) : (
            <div class="dim small hw-col-sub hw-col-sub--empty">no sensor</div>
          )}
        </div>
      </div>

      {hi != null && lo != null && (
        <div class="thermo">
          <div class="thermo-track"><span class="thermo-mark" style={{ left: `${markPct}%` }} /></div>
          <div class="thermo-row">
            <span class="t-lo">L {Math.round(lo)}°</span>
            <span class="dim small">next 24h</span>
            <span class="t-hi">H {Math.round(hi)}°</span>
          </div>
        </div>
      )}

      {/* Next 3 days — fills the gap between the range and the solar progress. */}
      <ForecastStrip weather={weather} />

      {solarTotal > 0 && (
        <div class="solar-prog">
          <div class="flex between items-baseline solar-prog-head">
            <span class="rate-k weather-eyebrow"><Icon name="solar" size={13} style={{ color: "var(--pv)" }} />Solar today</span>
            <span class="rate-sub"><b class="solar-done">{solarDone.toFixed(1)}</b> / {solarTotal.toFixed(1)} kWh forecast</span>
          </div>
          <div class="solar-prog-track"><div class="solar-prog-fill" style={{ width: `${solarPct}%` }} /></div>
          <div class="thermo-row">
            <span class="dim small">{solarDone - solarTotal > 0.3 ? `beat forecast +${(solarDone - solarTotal).toFixed(1)} kWh` : `${solarPct}% generated`}</span>
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
