import { useEffect, useState } from "preact/hooks";
import type { DaikinDevice, ActionResult } from "../../lib/types";
import { setTankTemperature, setTankPower, setLwtOffset, setClimatePower } from "../../lib/endpoints";
import { Toggle } from "../common/Inputs";
import { Modal } from "../common/Modal";
import { Icon } from "../common/Icon";
import { toast } from "../../lib/toast";

interface HeatingControlsProps {
  dev: DaikinDevice | null;
  // control_mode from /daikin/quota — lets the lock + active state show even
  // when device telemetry is cold (Daikin quota), since `dev` would be null.
  controlMode?: string | null;
  onChanged: () => void;
}

const TANK_MIN = 30, TANK_MAX = 65;
const LWT_MIN = -10, LWT_MAX = 10;

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

// Manual Daikin controls, modelled on the Onecta app and split into two cards —
// Climate (space-heating power + leaving-water offset) and Tank/DHW (power +
// target). Locked by default (the heat pump runs on dhw_policy); unlocking
// needs a confirmation modal — that consent is the gate, so the controls then
// apply directly (no per-action confirm).
export function HeatingControls({ dev, controlMode, onChanged }: HeatingControlsProps) {
  const active = (dev?.control_mode ?? controlMode) === "active";
  const [unlocked, setUnlocked] = useState(false);
  const [confirmingUnlock, setConfirmingUnlock] = useState(false);
  const [busy, setBusy] = useState(false);
  const [tankTarget, setTankTarget] = useState<number>(dev?.tank_target ?? 45);
  const [lwt, setLwt] = useState<number>(dev?.lwt_offset ?? 0);

  // Re-sync editable fields to the device's confirmed values (after a write
  // refreshes status, or the optimizer writes externally).
  useEffect(() => { if (dev?.tank_target != null) setTankTarget(dev.tank_target); }, [dev?.tank_target]);
  useEffect(() => { if (dev?.lwt_offset != null) setLwt(dev.lwt_offset); }, [dev?.lwt_offset]);

  const editable = active && unlocked;

  // Single write helper — the unlock modal was the consent, so no per-action
  // confirm. Refreshes + toasts on completion.
  const run = async (label: string, fn: () => Promise<ActionResult>) => {
    if (busy) return;
    setBusy(true);
    try {
      const res = await fn();
      toast.success(res.message || label);
      onChanged();
    } catch (e) {
      toast.error("Daikin command failed", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  // Correct power semantics: climate_on / dhw_on are what /daikin/status serves
  // (tank_power is legacy and never populated → it always read OFF before).
  const climateOn = dev?.climate_on ?? dev?.is_on ?? false;
  const tankOn = dev?.dhw_on ?? dev?.tank_power ?? false;

  // Require a KNOWN device value that differs — otherwise (value absent) we'd let
  // an Apply fire a redundant no-op write to the heat pump, burning Daikin quota.
  const tankChanged = editable && !busy && dev?.tank_target != null && tankTarget !== dev.tank_target;
  const lwtChanged = editable && !busy && dev?.lwt_offset != null && lwt !== dev.lwt_offset;

  return (
    <div class="heating-controls">
      <div class="heating-controls-head">
        <span class="heating-controls-title">Controls</span>
        {dev && !active && (
          <span class="heating-controls-passive" title="HEM is in passive mode — it observes Daikin but never writes. Set DAIKIN_CONTROL_MODE to active in Settings to drive the heat pump from here.">
            passive · read-only
          </span>
        )}
      </div>

      {active && (
        <div class={`hc-lockbar${unlocked ? " hc-lockbar--open" : ""}`}>
          <span class="hc-lockbar-icon" aria-hidden="true">{unlocked ? "🔓" : "🔒"}</span>
          <span class="hc-lockbar-text">
            {unlocked
              ? "Manual control on — what you apply is written to the heat pump."
              : "Locked — the tank follows the automatic schedule."}
          </span>
          <button type="button" class={`btn btn--sm${unlocked ? " btn--ghost" : " btn--primary"}`}
                  aria-pressed={unlocked}
                  onClick={() => (unlocked ? setUnlocked(false) : setConfirmingUnlock(true))}>
            {unlocked ? "Re-lock" : "Take control"}
          </button>
        </div>
      )}

      <div class={`heating-controls-cards${active && !unlocked ? " is-locked" : ""}`}>
        {/* Climate (space heating) — power + leaving-water offset */}
        <section class="hc-card">
          <header class="hc-card-head">
            <span class="hc-card-icon"><Icon name="power-live" size={14} /></span>
            <span class="hc-card-title">Climate</span>
            <span class={`hc-card-state${climateOn ? " is-on" : ""}`}>{climateOn ? "ON" : "OFF"}</span>
            <Toggle value={climateOn} ariaLabel="Climate power"
                    onChange={(next) => editable && run(`Climate ${next ? "ON" : "OFF"}`, () => setClimatePower(next))} />
          </header>
          <div class="hc-card-body">
            <span class="hc-card-label">Water offset</span>
            <Slider value={lwt} min={LWT_MIN} max={LWT_MAX} step={0.5} current={dev?.lwt_offset}
                    unit="°" tone="cool" disabled={!editable || busy}
                    onInput={(v) => setLwt(clamp(Math.round(v * 2) / 2, LWT_MIN, LWT_MAX))} />
            <ApplyBtn changed={lwtChanged} label={`Set ${lwt >= 0 ? "+" : ""}${lwt}°`}
                      onClick={() => run(`LWT offset ${lwt >= 0 ? "+" : ""}${lwt}`, () => setLwtOffset(lwt))} />
          </div>
        </section>

        {/* Tank (DHW) — power + target */}
        <section class="hc-card">
          <header class="hc-card-head">
            <span class="hc-card-icon"><Icon name="schedule" size={14} /></span>
            <span class="hc-card-title">Tank</span>
            <span class={`hc-card-state${tankOn ? " is-on" : ""}`}>{tankOn ? "ON" : "OFF"}</span>
            <Toggle value={tankOn} ariaLabel="Tank power"
                    onChange={(next) => editable && run(`Tank ${next ? "ON" : "OFF"}`, () => setTankPower(next))} />
          </header>
          <div class="hc-card-body">
            <span class="hc-card-label">Target</span>
            <Slider value={tankTarget} min={TANK_MIN} max={TANK_MAX} step={1} current={dev?.tank_target}
                    unit="°C" tone="thermal" disabled={!editable || busy}
                    onInput={(v) => setTankTarget(clamp(Math.round(v), TANK_MIN, TANK_MAX))} />
            <ApplyBtn changed={tankChanged} label={`Set ${tankTarget}°C`}
                      onClick={() => run(`Tank set to ${tankTarget}°C`, () => setTankTemperature(tankTarget))} />
          </div>
        </section>
      </div>

      <Modal open={confirmingUnlock} onClose={() => setConfirmingUnlock(false)} width="sm"
             title="Enable manual control?"
             footer={
               <>
                 <button class="btn btn--ghost" onClick={() => setConfirmingUnlock(false)}>Cancel</button>
                 <button class="btn btn--primary" onClick={() => { setUnlocked(true); setConfirmingUnlock(false); }}>
                   Enable
                 </button>
               </>
             }>
        <p>Unlock the heat-pump controls. While unlocked, changes you apply are
           written directly to the unit via Onecta.</p>
        <p class="muted heating-controls-hint">The tank otherwise follows the
           automatic schedule — re-lock when you're done.</p>
      </Modal>
    </div>
  );
}

// Slider with a tick marking the device's CURRENT value, so you see where you're
// dragging relative to where the unit is now. Native range for a11y + touch.
function Slider({ value, min, max, step = 1, current, unit, tone = "thermal", disabled, onInput }: {
  value: number; min: number; max: number; step?: number; current?: number | null;
  unit: string; tone?: "thermal" | "cool"; disabled?: boolean; onInput: (v: number) => void;
}) {
  const fmt = (v: number) => (v % 1 === 0 ? String(v) : v.toFixed(1));
  const pct = (v: number) => Math.max(0, Math.min(1, (v - min) / (max - min))) * 100;
  const curValid = current != null && Number.isFinite(current);
  return (
    <div class={`hc-slider hc-slider--${tone}${disabled ? " is-disabled" : ""}`}>
      <div class="hc-slider-rail">
        <div class="hc-slider-fill" style={{ width: `${pct(value)}%` }} />
        {curValid && (
          <span class="hc-slider-cur" style={{ left: `${pct(current as number)}%` }}
                title={`now ${fmt(current as number)}${unit}`} />
        )}
        <input class="hc-slider-input" type="range" min={min} max={max} step={step}
               value={value} disabled={disabled} aria-label={`Set value (${unit})`}
               onInput={(e) => onInput(Number((e.target as HTMLInputElement).value))} />
      </div>
      <div class="hc-slider-readout">
        <span class="hc-slider-value">{fmt(value)}{unit}</span>
        {curValid && value !== current && (
          <span class="hc-slider-from">from {fmt(current as number)}{unit}</span>
        )}
      </div>
    </div>
  );
}

// Apply button — primary + pending value when there's a change, quiet otherwise.
function ApplyBtn({ changed, label, onClick }: { changed: boolean; label: string; onClick: () => void }) {
  return (
    <button class={`btn btn--sm hc-apply${changed ? " btn--primary hc-apply--ready" : ""}`}
            disabled={!changed} onClick={onClick}>
      {changed ? label : "Apply"}
    </button>
  );
}
