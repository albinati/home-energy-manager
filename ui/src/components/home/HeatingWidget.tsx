import type { CockpitState, DaikinDevice, ApiQuotaResponse, EnergyReport } from "../../lib/types";
import { tempC, kwh } from "../../lib/format";
import { Pill } from "../common/Pill";
import "./heating.css";

interface HeatingWidgetProps {
  state: CockpitState;
  daikin: DaikinDevice[] | null;
  daikinQuota: ApiQuotaResponse | null;
  report: EnergyReport | null;
}

// Heating-focused panel: tank status (current temp + target + on/off),
// space heating mode + setpoint, today's DHW vs space split from
// /energy/report.heating, and the Daikin daily quota indicator so the
// operator knows whether they can hit Daikin live.
export function HeatingWidget({ state, daikin, daikinQuota, report }: HeatingWidgetProps) {
  const dev = daikin && daikin.length > 0 ? daikin[0] : null;
  const mode = (state.daikin_mode || dev?.mode || "").toLowerCase();
  const isHeating = mode.includes("heat");
  const isCooling = mode.includes("cool");
  const isOff = mode === "" || mode === "off" || mode === "idle";

  const tankTemp = state.tank_c ?? dev?.tank_temp ?? null;
  const tankTarget = dev?.tank_target ?? null;
  const tankPower = dev?.tank_power ?? null;
  const indoorTemp = state.indoor_c ?? dev?.room_temp ?? null;
  const indoorTarget = dev?.target_temp ?? null;

  const dhwKwh = report?.heating?.kwh_for_showers ?? null;
  const spaceKwh = report?.heating?.kwh_for_heating ?? null;

  const quotaUsed = daikinQuota?.quota_used_24h ?? null;
  const quotaBudget = daikinQuota?.daily_budget ?? null;
  const quotaPct = quotaUsed != null && quotaBudget != null && quotaBudget > 0
    ? (quotaUsed / quotaBudget) * 100
    : null;
  const quotaTone = quotaPct == null ? "neutral" : quotaPct > 85 ? "bad" : quotaPct > 60 ? "warn" : "ok";

  return (
    <div class="heating">
      <div class="heating-header">
        <div class={`heating-mode heating-mode--${isHeating ? "heating" : isCooling ? "cooling" : "idle"}`}>
          {isHeating && <span class="heating-mode-icon">🔥</span>}
          {isCooling && <span class="heating-mode-icon">❄</span>}
          {isOff && <span class="heating-mode-icon">⏸</span>}
          <span>{state.daikin_mode || "—"}</span>
        </div>
        {quotaBudget != null && (
          <Pill tone={quotaTone === "ok" ? "ok" : quotaTone === "warn" ? "warn" : quotaTone === "bad" ? "bad" : "dim"}
                title={`Daikin daily quota — ${quotaUsed}/${quotaBudget} calls used`}>
            Daikin {quotaUsed}/{quotaBudget}
          </Pill>
        )}
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
          <span class="heating-row-icon">🏠</span>
          <div class="heating-row-body">
            <div class="heating-row-label">Indoor</div>
            <div class="heating-row-sub">{indoorTarget != null ? `setpoint ${tempC(indoorTarget, 0)}` : ""}</div>
          </div>
          <span class="heating-row-temp">{tempC(indoorTemp, 0)}</span>
        </div>

        <div class="heating-row">
          <span class="heating-row-icon">💧</span>
          <div class="heating-row-body">
            <div class="heating-row-label">LWT</div>
            <div class="heating-row-sub">leaving water</div>
          </div>
          <span class="heating-row-temp">{tempC(state.lwt_c, 0)}</span>
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
