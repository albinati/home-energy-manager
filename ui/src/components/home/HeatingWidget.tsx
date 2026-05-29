import type {
  CockpitState,
  DaikinDevice,
  ApiQuotaResponse,
  EnergyReport,
  WeatherResponse,
  ExecutionTodayResponse,
  ExecutionSlot,
} from "../../lib/types";
import { useState } from "preact/hooks";
import { kwh, relTime } from "../../lib/format";
import { forceRefreshDaikin } from "../../lib/endpoints";
import { Pill } from "../common/Pill";
import { Gauge } from "../common/Gauge";
import { RadialGauge } from "../common/RadialGauge";
import { Modal } from "../common/Modal";
import { HeatingControls } from "./HeatingControls";
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
}

// Tank / outdoor / LWT + Daikin mode + cache freshness + quota.
// Outdoor temp + LWT now prefer /execution/today (logged Daikin readings,
// no live API call) over the cached /daikin/status — same data freshness,
// zero quota cost.
export function HeatingWidget({ state, daikin, daikinQuota, report, weather, execution, onRefresh }: HeatingWidgetProps) {
  const dev = daikin && daikin.length > 0 ? daikin[0] : null;
  // Explicit, confirmed LIVE read. Everything on this widget normally renders
  // the cache the LP/scheduler already refreshed (~30 min cadence) — we only
  // hit the Daikin API on demand, behind this confirm, to protect the ~200/day
  // quota. quota-blocked → backend returns warm cache (no network).
  const [confirmingRefresh, setConfirmingRefresh] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  async function doForceRefresh() {
    setRefreshing(true);
    try {
      await forceRefreshDaikin();
    } catch { /* surfaced by the re-fetch below; keep the UI responsive */ }
    finally {
      setConfirmingRefresh(false);
      setRefreshing(false);
      onRefresh?.();
    }
  }
  // No cooling on this system — only heating + DHW. We surface compressor
  // status via the tank/space rows themselves (ON/OFF), not a "mode" chip.
  const tankTemp = state.tank_c ?? dev?.tank_temp ?? null;
  const tankTarget = dev?.tank_target ?? null;
  const tankPower = dev?.tank_power ?? null;

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
        <button class="btn btn--ghost btn--sm heating-refresh" disabled={refreshing}
                title="Fetch live data from the heat pump now (uses one Daikin API call)"
                onClick={() => setConfirmingRefresh(true)}>
          {refreshing ? "…" : "↻ Live"}
        </button>
      </div>

      <RadialGauge label={`Tank${tankPower != null ? (tankPower ? " · on" : " · off") : ""}`}
                   value={tankTemp} min={20} max={65} target={tankTarget} tone="thermal" />
      <div class="heating-gauges heating-gauges--secondary">
        <Gauge label="Outdoor" value={outdoorTemp} min={-5} max={35} tone="cool"
               sub={outdoorSource === "execution" ? "Daikin sensor (logged)"
                    : outdoorSource === "daikin" ? "Daikin sensor (live)"
                    : "Open-Meteo forecast"} />
        <Gauge label="LWT" value={lwt} min={20} max={55} tone="thermal" sub="leaving water" />
      </div>

      {totalHeatingKwh != null && (
        <div class="heating-split">
          <div class="heating-split-label">Today's heating energy</div>
          <div class="heating-split-row">
            <div class="heating-split-item">
              <span class="heating-split-dot heating-split-dot--dhw" />
              <span class="heating-split-name">Total estimate</span>
              <span class="heating-split-value">{kwh(totalHeatingKwh)}</span>
            </div>
          </div>
        </div>
      )}

      <HeatingControls dev={dev} controlMode={daikinQuota?.control_mode} onChanged={() => onRefresh?.()} />

      <Modal open={confirmingRefresh} onClose={() => setConfirmingRefresh(false)} width="sm"
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
      </Modal>
    </div>
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
