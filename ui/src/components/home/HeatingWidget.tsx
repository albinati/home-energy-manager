import type {
  CockpitState,
  DaikinDevice,
  ApiQuotaResponse,
  EnergyReport,
  WeatherResponse,
  ExecutionTodayResponse,
  ExecutionSlot,
} from "../../lib/types";
import { useState, useEffect } from "preact/hooks";
import { kwh, relTime } from "../../lib/format";
import { forceRefreshDaikin, getDhwSchedule, getLwtSchedule } from "../../lib/endpoints";
import type { DhwScheduleRow, LwtScheduleRow } from "../../lib/types";
import { Pill } from "../common/Pill";
import { Gauge } from "../common/Gauge";
import { RadialGauge } from "../common/RadialGauge";
import { Modal } from "../common/Modal";
import { HeatingControls } from "./HeatingControls";
import { TankScheduleBadges } from "../common/TankScheduleBadges";
import { LwtScheduleBadges } from "../common/LwtScheduleBadges";
import { RefreshCountdown } from "../common/RefreshCountdown";
import "./heating.css";

interface HeatingWidgetProps {
  state: CockpitState;
  daikin: DaikinDevice[] | null;
  daikinQuota: ApiQuotaResponse | null;
  report: EnergyReport | null;
  weather: WeatherResponse | null;
  execution: ExecutionTodayResponse | null;
  // Shared DHW schedule (today+tomorrow). When omitted the widget self-fetches.
  dhwSchedule?: DhwScheduleRow[] | null;
  // Re-fetch Daikin status + quota after a manual control write.
  onRefresh?: () => void;
}

// Tank / outdoor / LWT + Daikin mode + cache freshness + quota.
// Outdoor temp + LWT now prefer /execution/today (logged Daikin readings,
// no live API call) over the cached /daikin/status — same data freshness,
// zero quota cost.
export function HeatingWidget({ state, daikin, daikinQuota, report, weather, execution, dhwSchedule, onRefresh }: HeatingWidgetProps) {
  const dev = daikin && daikin.length > 0 ? daikin[0] : null;
  // Explicit, confirmed LIVE read. Everything on this widget normally renders
  // the cache the LP/scheduler already refreshed (~30 min cadence) — we only
  // hit the Daikin API on demand, behind this confirm, to protect the ~200/day
  // quota. quota-blocked → backend returns warm cache (no network).
  const [confirmingRefresh, setConfirmingRefresh] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  // Manual force-refresh cooldown — locks the button (and counts down) in
  // lock-step with the server's per-actor throttle, so you can't re-click for
  // a few minutes and silently burn a wasted (throttled) call.
  const forceIv = daikinQuota?.force_refresh_min_interval_seconds ?? 300;
  const serverAvailIn = daikinQuota?.force_refresh_available_in_seconds ?? 0;
  const [cooldownUntil, setCooldownUntil] = useState(0);  // epoch ms
  const [nowMs, setNowMs] = useState(() => Date.now());
  // Seed from the server (covers page reloads mid-cooldown); never lower it.
  useEffect(() => {
    if (serverAvailIn > 0) {
      setCooldownUntil((prev) => Math.max(prev, Date.now() + serverAvailIn * 1000));
    }
  }, [serverAvailIn]);
  // Tick once a second while the lock is active.
  useEffect(() => {
    if (cooldownUntil <= Date.now()) return;
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [cooldownUntil]);
  const cooldownLeft = Math.max(0, Math.ceil((cooldownUntil - nowMs) / 1000));
  const onCooldown = cooldownLeft > 0;
  async function doForceRefresh() {
    setRefreshing(true);
    setRefreshError(null);
    try {
      await forceRefreshDaikin();
      setConfirmingRefresh(false);
      setCooldownUntil(Date.now() + forceIv * 1000);  // lock the button
      onRefresh?.();  // re-pull the now-fresh cache
    } catch (e) {
      // Keep the modal open so a failed live read isn't mistaken for success.
      setRefreshError(e instanceof Error ? e.message : "Live read failed");
    } finally {
      setRefreshing(false);
    }
  }
  // No cooling on this system — only heating + DHW. We surface compressor
  // status via the tank/space rows themselves (ON/OFF), not a "mode" chip.
  const tankTemp = state.tank_c ?? dev?.tank_temp ?? null;
  const tankTarget = dev?.tank_target ?? null;
  // dhw_on is what /daikin/status serves; tank_power is legacy (never populated).
  const tankPower = dev?.dhw_on ?? dev?.tank_power ?? null;

  // Deterministic tank plan (today+tomorrow times + targets) — dhw_policy, zero
  // quota. Prefer the shared prop from the parent; self-fetch as a fallback.
  const [selfSchedule, setSelfSchedule] = useState<DhwScheduleRow[]>([]);
  useEffect(() => {
    if (dhwSchedule) return;  // provided by parent
    let alive = true;
    getDhwSchedule().then((r) => { if (alive) setSelfSchedule(r.rows || []); }).catch(() => {});
    return () => { alive = false; };
  }, [dhwSchedule]);
  const scheduleAll = dhwSchedule ?? selfSchedule;
  // Drop windows that have already finished — a boost/warmup that ran earlier
  // today is noise here; show only what's ongoing or still upcoming.
  const schedule = scheduleAll.filter((r) => {
    const end = r.end_utc ? Date.parse(r.end_utc) : NaN;
    return Number.isNaN(end) || end >= Date.now();
  });

  // Committed LWT-offset pre-heat plan (#481) — boost/setback windows, same
  // deterministic-schedule pattern as the tank plan, zero Daikin quota. Empty
  // when DAIKIN_LWT_PREHEAT_ENABLED is off (climate hands-off).
  const [lwtSchedule, setLwtSchedule] = useState<LwtScheduleRow[]>([]);
  useEffect(() => {
    let alive = true;
    getLwtSchedule().then((r) => { if (alive) setLwtSchedule(r.rows || []); }).catch(() => {});
    return () => { alive = false; };
  }, []);
  const lwtPlan = lwtSchedule.filter((r) => {
    const end = r.end_utc ? Date.parse(r.end_utc) : NaN;
    return Number.isNaN(end) || end >= Date.now();
  });

  // LWT: latest execution slot first, then live cockpit state.
  const lwtFromExec = latestExecValue(execution, (s) => s.daikin_lwt_c);
  const lwt = lwtFromExec ?? state.lwt_c ?? dev?.lwt ?? null;

  // Outdoor: 1) execution_today logged Daikin sensor (fresh, free)
  //          2) cached Daikin device sensor
  //          3) Daikin echo in /weather
  //          4) Open-Meteo forecast slot closest to now
  let outdoorTemp = latestExecValue(execution, (s) => s.daikin_outdoor_c);
  let outdoorSource: "execution" | "daikin" | "openmeteo" = "execution";
  if (outdoorTemp == null) {
    outdoorTemp = dev?.outdoor_temp ?? weather?.daikin?.outdoor_temp ?? null;
    outdoorSource = "daikin";
  }
  if (outdoorTemp == null && weather?.forecast && weather.forecast.length > 0) {
    const nowTs = Date.now();
    let closest = weather.forecast[0];
    let closestDist = Math.abs(Date.parse(closest.time) - nowTs);
    for (const f of weather.forecast) {
      const d = Math.abs(Date.parse(f.time) - nowTs);
      if (d < closestDist) { closest = f; closestDist = d; }
    }
    outdoorTemp = closest.temp_c ?? null;
    outdoorSource = "openmeteo";
  }

  // /energy/report?period=day doesn't carry a DHW vs space heating split —
  // only a single heating_estimate_kwh total. Show that when present.
  const totalHeatingKwh = report?.heating_estimate_kwh ?? null;

  const quotaUsed = daikinQuota?.quota_used_24h ?? null;
  const quotaBudget = daikinQuota?.daily_budget ?? null;
  const quotaPct = quotaUsed != null && quotaBudget != null && quotaBudget > 0
    ? (quotaUsed / quotaBudget) * 100
    : null;
  const quotaTone = quotaPct == null ? "neutral" : quotaPct > 85 ? "bad" : quotaPct > 60 ? "warn" : "ok";

  const cacheAge = daikinQuota?.cache_age_seconds;
  const lastRefresh = daikinQuota?.last_refresh_at_utc;
  const freshLabel = lastRefresh ? relTime(lastRefresh) :
                    cacheAge != null ? `${Math.round(cacheAge / 60)}m ago` :
                    null;

  return (
    <div class="heating">
      <div class="heating-header">
        {freshLabel && (
          <span class="heating-freshness" title={`Daikin cache last refreshed ${freshLabel}`}>
            Cache · {freshLabel}
          </span>
        )}
        {quotaBudget != null && (
          <Pill tone={quotaTone === "ok" ? "ok" : quotaTone === "warn" ? "warn" : quotaTone === "bad" ? "bad" : "dim"}
                title={`Daikin API — ${quotaUsed}/${quotaBudget} calls in the last 24h (Daikin enforces ~200/day, resets midnight UTC)`}>
            {quotaUsed}/{quotaBudget} · 24h
          </Pill>
        )}
        <RefreshCountdown
          lastFetchAt={onCooldown ? cooldownUntil - forceIv * 1000 : null}
          intervalMs={forceIv * 1000}
          loading={refreshing}
          disabled={onCooldown}
          onRefresh={() => setConfirmingRefresh(true)}
          label={onCooldown ? undefined : "Live"} />
      </div>

      <RadialGauge label={`Tank${tankPower != null ? (tankPower ? " · on" : " · off") : ""}`}
                   value={tankTemp} min={20} max={65} target={tankTarget} tone="thermal" />
      <div class="heating-gauges heating-gauges--secondary">
        <Gauge label="Outdoor" value={outdoorTemp} min={-5} max={40}
               fillColor={tempColor(outdoorTemp)}
               icon={<ThermometerIcon />} showFahrenheit
               sub={outdoorSource === "execution" ? "Daikin sensor (logged)"
                    : outdoorSource === "daikin" ? "Daikin sensor (live)"
                    : "Open-Meteo forecast"} />
        <Gauge label="LWT" value={lwt} min={20} max={55} tone="thermal" showFahrenheit sub="leaving water" />
      </div>

      {totalHeatingKwh != null && (
        <div class="heating-split">
          <div class="heating-split-label">Today's heating energy</div>
          <div class="heating-energy-value" title="Estimated total heat-pump electricity today (DHW + space, not split by the meter)">
            {kwh(totalHeatingKwh)}
            <span class="heating-energy-est">est</span>
          </div>
        </div>
      )}

      {schedule.length > 0 && (
        <div class="heating-plan">
          <div class="heating-plan-title">Tank plan</div>
          <TankScheduleBadges rows={schedule} />
        </div>
      )}

      {lwtPlan.length > 0 && (
        <div class="heating-plan">
          <div class="heating-plan-title">Heating plan · LWT offset</div>
          <LwtScheduleBadges rows={lwtPlan} />
        </div>
      )}

      <HeatingControls dev={dev} controlMode={daikinQuota?.control_mode} onChanged={() => onRefresh?.()} />

      <Modal open={confirmingRefresh} onClose={() => { setConfirmingRefresh(false); setRefreshError(null); }} width="sm"
             title="Fetch live heat-pump data?"
             footer={
               <>
                 <button class="btn btn--ghost" disabled={refreshing}
                         onClick={() => setConfirmingRefresh(false)}>Cancel</button>
                 <button class="btn btn--primary" disabled={refreshing} onClick={doForceRefresh}>
                   {refreshing ? "Refreshing…" : "Refresh now"}
                 </button>
               </>
             }>
        <p>This reads the tank, leaving-water and outdoor temperatures straight
           from the heat pump — one of the limited <strong>~200 Daikin API
           calls/day</strong>.</p>
        <p class="muted heating-controls-hint">You normally don't need this: the
           planner already refreshes these values about every 30 minutes, and
           everything here shows that reading.</p>
        {refreshError && <p class="heating-refresh-error">Live read failed: {refreshError}</p>}
      </Modal>
    </div>
  );
}

// Continuous outdoor-temperature colour: light-blue (cold) → dark-blue → green
// (mild, ~18°C) → yellow → red → purple (very hot). Interpolated in HSL along
// the shortest hue path so the midpoints stay vivid (RGB lerp would go muddy).
function tempColor(t: number | null): string | undefined {
  if (t == null || !Number.isFinite(t)) return undefined;
  // [temp, hue, sat%, light%]
  const stops: [number, number, number, number][] = [
    [0, 195, 90, 72],   // light blue
    [10, 222, 78, 48],  // dark blue
    [25, 48, 95, 55],   // yellow
    [30, 6, 85, 55],    // red
    [40, 285, 65, 55],  // purple
  ];
  const c = Math.max(stops[0][0], Math.min(stops[stops.length - 1][0], t));
  let a = stops[0], b = stops[stops.length - 1];
  for (let i = 0; i < stops.length - 1; i++) {
    if (c >= stops[i][0] && c <= stops[i + 1][0]) { a = stops[i]; b = stops[i + 1]; break; }
  }
  const f = b[0] === a[0] ? 0 : (c - a[0]) / (b[0] - a[0]);
  // Shortest-path hue interpolation.
  let dh = b[1] - a[1];
  if (dh > 180) dh -= 360;
  if (dh < -180) dh += 360;
  const h = ((a[1] + dh * f) % 360 + 360) % 360;
  const s = a[2] + (b[2] - a[2]) * f;
  const l = a[3] + (b[3] - a[3]) * f;
  return `hsl(${h.toFixed(0)} ${s.toFixed(0)}% ${l.toFixed(0)}%)`;
}

function ThermometerIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M14 14.76V5a2 2 0 0 0-4 0v9.76a4 4 0 1 0 4 0z" />
    </svg>
  );
}

function latestExecValue(
  exec: ExecutionTodayResponse | null,
  pick: (s: ExecutionSlot) => number | null | undefined,
): number | null {
  if (!exec?.slots || exec.slots.length === 0) return null;
  const sorted = exec.slots.slice().sort((a, b) => (b.slot_utc ?? "").localeCompare(a.slot_utc ?? ""));
  for (const s of sorted) {
    const v = pick(s);
    if (v != null && Number.isFinite(v)) return v;
  }
  return null;
}
