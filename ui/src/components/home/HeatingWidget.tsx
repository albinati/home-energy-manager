import type {
  CockpitState,
  DaikinDevice,
  ApiQuotaResponse,
  EnergyReport,
  WeatherResponse,
  ExecutionTodayResponse,
  ExecutionSlot,
} from "../../lib/types";
import type { ComponentChildren } from "preact";
import { useState, useEffect } from "preact/hooks";
import { kwh, relTime } from "../../lib/format";
import { forceRefreshDaikin } from "../../lib/endpoints";
import { Pill } from "../common/Pill";
import { RadialGauge } from "../common/RadialGauge";
import { Modal } from "../common/Modal";
import { RefreshCountdown } from "../common/RefreshCountdown";
import "./heating.css";

interface HeatingWidgetProps {
  state: CockpitState;
  daikin: DaikinDevice[] | null;
  daikinQuota: ApiQuotaResponse | null;
  report: EnergyReport | null;
  weather: WeatherResponse | null;
  execution: ExecutionTodayResponse | null;
  // Re-fetch Daikin status + quota after a manual control write.
  onRefresh?: () => void;
  // The heating-plan timeline slot — redesign order puts the plan chart first
  // (it's the hero of this card); the gauges demote to a row beneath it.
  children?: ComponentChildren;
}

// Tank / outdoor / LWT + Daikin mode + cache freshness + quota.
// Outdoor temp + LWT now prefer /execution/today (logged Daikin readings,
// no live API call) over the cached /daikin/status — same data freshness,
// zero quota cost.
export function HeatingWidget({ state, daikin, daikinQuota, report, weather, execution, onRefresh, children }: HeatingWidgetProps) {
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

  // The tank + LWT-offset PLAN moved to the dedicated "Heating plan" timeline
  // widget (D-1/D/D+1). This card keeps the live gauges + manual controls.

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
          <span class="heating-freshness"
                title={`These readings come from the heat pump's last automatic refresh (${freshLabel}); the planner re-reads it about every 30 minutes.`}>
            Data from {freshLabel}
          </span>
        )}
        {quotaBudget != null && (
          <Pill tone={quotaTone === "ok" ? "ok" : quotaTone === "warn" ? "warn" : quotaTone === "bad" ? "bad" : "dim"}
                title={`Daikin API — ${quotaUsed} of ${quotaBudget} calls used in the last 24h (Daikin enforces ~200/day, resets midnight UTC). A manual refresh costs 1 call.`}>
            {quotaUsed}/{quotaBudget} calls
          </Pill>
        )}
        <RefreshCountdown
          lastFetchAt={onCooldown ? cooldownUntil - forceIv * 1000 : null}
          intervalMs={forceIv * 1000}
          loading={refreshing}
          disabled={onCooldown}
          onRefresh={() => setConfirmingRefresh(true)}
          label={onCooldown ? undefined : "Refresh · 1 call"} />
      </div>

      {/* Heating-plan timeline — the hero of this card (redesign). */}
      {children}

      {/* Gauges demote to a thin accessory row beneath the chart: three radial
          dials, domain-coloured — outdoor (cool blue) · tank (amber heat) ·
          LWT (house/radiators). */}
      <div class="heat-gauges">
        {state.indoor_c != null && (
          <div title={`House room sensors — mean of ${state.indoor?.n_rooms ?? 1} room(s)`}>
            <RadialGauge label="Indoor" value={state.indoor_c} min={10} max={30}
                         color="var(--thermal)" sub={cAndF(state.indoor_c)} />
          </div>
        )}
        <div title={outdoorSource === "execution" ? "Daikin sensor (logged)"
                    : outdoorSource === "daikin" ? "Daikin sensor (live)"
                    : "Open-Meteo forecast"}>
          <RadialGauge label="Outdoor" value={outdoorTemp} min={-5} max={35}
                       color="var(--grid)" sub={cAndF(outdoorTemp)} />
        </div>
        <RadialGauge label={`Tank${tankPower != null ? (tankPower ? " · on" : " · off") : ""}`}
                     value={tankTemp} min={20} max={65} target={tankTarget} color="var(--peak)"
                     sub={tankTarget != null ? `set ${Math.round(tankTarget)}°` : undefined} />
        <RadialGauge label="LWT" value={lwt} min={20} max={55}
                     color="var(--house)" sub={cAndF(lwt) ?? "leaving water"} />
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

// "16°C · 61°F" gauge subline (redesign).
function cAndF(c: number | null): string | undefined {
  if (c == null || !Number.isFinite(c)) return undefined;
  return `${Math.round(c)}°C · ${Math.round(c * 9 / 5 + 32)}°F`;
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
