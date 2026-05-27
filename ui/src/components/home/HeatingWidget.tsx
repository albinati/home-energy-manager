import type { CockpitState, DaikinDevice, ApiQuotaResponse, EnergyReport, WeatherResponse } from "../../lib/types";
import { tempC, kwh, relTime } from "../../lib/format";
import { Pill } from "../common/Pill";
import "./heating.css";

interface HeatingWidgetProps {
  state: CockpitState;
  daikin: DaikinDevice[] | null;
  daikinQuota: ApiQuotaResponse | null;
  report: EnergyReport | null;
  weather: WeatherResponse | null;
}

// Heating-focused panel: tank (current + target + on/off), outdoor temp
// (from Daikin sensor), LWT, mode pill, today's DHW vs space heating split,
// Daikin daily quota indicator + cache freshness.
export function HeatingWidget({ state, daikin, daikinQuota, report, weather }: HeatingWidgetProps) {
  const dev = daikin && daikin.length > 0 ? daikin[0] : null;
  const mode = (state.daikin_mode || dev?.mode || "").toLowerCase();
  const isHeating = mode.includes("heat");
  const isCooling = mode.includes("cool");
  const isOff = mode === "" || mode === "off" || mode === "idle";

  const tankTemp = state.tank_c ?? dev?.tank_temp ?? null;
  const tankTarget = dev?.tank_target ?? null;
  const tankPower = dev?.tank_power ?? null;
  const lwt = state.lwt_c ?? dev?.lwt ?? null;

  // Outdoor: prefer Daikin's sensor (most accurate microclimate), fall back
  // to /weather's daikin echo, then to /weather's current forecast slot.
  let outdoorTemp = dev?.outdoor_temp ?? weather?.daikin?.outdoor_temp ?? null;
  let outdoorSource: "daikin" | "openmeteo" = "daikin";
  if (outdoorTemp == null && weather?.forecast && weather.forecast.length > 0) {
    // Find the forecast slot closest to now
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

  const dhwKwh = report?.heating?.kwh_for_showers ?? null;
  const spaceKwh = report?.heating?.kwh_for_heating ?? null;

  const quotaUsed = daikinQuota?.quota_used_24h ?? null;
  const quotaBudget = daikinQuota?.daily_budget ?? null;
  const quotaPct = quotaUsed != null && quotaBudget != null && quotaBudget > 0
    ? (quotaUsed / quotaBudget) * 100
    : null;
  const quotaTone = quotaPct == null ? "neutral" : quotaPct > 85 ? "bad" : quotaPct > 60 ? "warn" : "ok";

  // Cache freshness — when did we last refresh Daikin from the cloud?
  const cacheAge = daikinQuota?.cache_age_seconds;
  const lastRefresh = daikinQuota?.last_refresh_at_utc;
  const freshLabel = lastRefresh ? relTime(lastRefresh) :
                    cacheAge != null ? `${Math.round(cacheAge / 60)}m ago` :
                    null;

  return (
    <div class="heating">
      <div class="heating-header">
        <div class={`heating-mode heating-mode--${isHeating ? "heating" : isCooling ? "cooling" : "idle"}`}>
          {isHeating && <span class="heating-mode-icon">🔥</span>}
          {isCooling && <span class="heating-mode-icon">❄</span>}
          {isOff && <span class="heating-mode-icon">⏸</span>}
          <span>{state.daikin_mode || "—"}</span>
        </div>
        <div class="heating-header-meta">
          {freshLabel && (
            <span class="heating-freshness" title={`Daikin cache last refreshed ${freshLabel}`}>
              {freshLabel}
            </span>
          )}
          {quotaBudget != null && (
            <Pill tone={quotaTone === "ok" ? "ok" : quotaTone === "warn" ? "warn" : quotaTone === "bad" ? "bad" : "dim"}
                  title={`Daikin daily quota — ${quotaUsed}/${quotaBudget} calls used`}>
              {quotaUsed}/{quotaBudget}
            </Pill>
          )}
        </div>
      </div>

      <div class="heating-rows">
        <div class="heating-row">
          <span class="heating-row-icon">♨</span>
          <div class="heating-row-body">
            <div class="heating-row-label">Tank</div>
            <div class="heating-row-sub">
              {tankTarget != null ? `target ${tempC(tankTarget, 0)}` : "no target"}
              {tankPower != null && (
                <> · <strong class={tankPower ? "heating-on" : "heating-off"}>
                  {tankPower ? "ON" : "OFF"}
                </strong></>
              )}
            </div>
          </div>
          <span class="heating-row-temp">{tempC(tankTemp, 0)}</span>
        </div>

        <div class="heating-row">
          <span class="heating-row-icon">🌡</span>
          <div class="heating-row-body">
            <div class="heating-row-label">Outdoor</div>
            <div class="heating-row-sub">{outdoorSource === "daikin" ? "Daikin sensor" : "Open-Meteo forecast"}</div>
          </div>
          <span class="heating-row-temp">{tempC(outdoorTemp, 0)}</span>
        </div>

        <div class="heating-row">
          <span class="heating-row-icon">💧</span>
          <div class="heating-row-body">
            <div class="heating-row-label">LWT</div>
            <div class="heating-row-sub">leaving water</div>
          </div>
          <span class="heating-row-temp">{tempC(lwt, 0)}</span>
        </div>
      </div>

      {(dhwKwh != null || spaceKwh != null) && (
        <div class="heating-split">
          <div class="heating-split-label">Today's heating energy</div>
          <div class="heating-split-row">
            <div class="heating-split-item">
              <span class="heating-split-dot heating-split-dot--dhw" />
              <span class="heating-split-name">DHW (tank)</span>
              <span class="heating-split-value">{dhwKwh != null ? kwh(dhwKwh) : "—"}</span>
            </div>
            <div class="heating-split-item">
              <span class="heating-split-dot heating-split-dot--space" />
              <span class="heating-split-name">Space</span>
              <span class="heating-split-value">{spaceKwh != null ? kwh(spaceKwh) : "—"}</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
