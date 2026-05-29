import { useEffect, useState } from "preact/hooks";
import type { DaikinDevice, ActionResult } from "../../lib/types";
import { setTankTemperature, setTankPower, setLwtOffset } from "../../lib/endpoints";
import { Toggle } from "../common/Inputs";
import { Modal } from "../common/Modal";
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

// Manual Daikin controls, modelled on the Onecta app: tank-target dial stepper,
// DHW power, LWT offset. Locked by default (the heat pump runs on dhw_policy);
// unlocking requires a confirmation modal — that consent is the gate, so the
// individual controls then apply directly (no per-action confirm).
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

  const dhwOn = dev?.tank_power ?? false;

  return (
    <div class="heating-controls">
      <div class="heating-controls-head">
        <span class="heating-controls-title">Controls</span>
        {dev && !active && (
          <span class="heating-controls-passive" title="HEM is in passive mode — it observes Daikin but never writes. Set DAIKIN_CONTROL_MODE to active in Settings to drive the heat pump from here.">
            passive · read-only
          </span>
        )}
        {active && (
          <button type="button"
                  class={`heating-controls-lock${unlocked ? " heating-controls-lock--open" : ""}`}
                  aria-pressed={unlocked}
                  title={unlocked ? "Manual control enabled — click to lock" : "Locked — click to enable manual control"}
                  onClick={() => (unlocked ? setUnlocked(false) : setConfirmingUnlock(true))}>
            {unlocked ? "🔓 editing" : "🔒 locked"}
          </button>
        )}
      </div>

      <div class="heating-control-row">
        <label class="heating-control-label">Tank target</label>
        <Stepper value={tankTarget} unit="°C" disabled={!editable || busy}
                 onStep={(d) => setTankTarget((v) => clamp(v + d, TANK_MIN, TANK_MAX))} />
        <button class="btn btn--sm" disabled={!editable || busy || tankTarget === dev?.tank_target}
                onClick={() => run(`Tank set to ${tankTarget}°C`, () => setTankTemperature(tankTarget))}>
          Apply
        </button>
      </div>

      <div class="heating-control-row">
        <label class="heating-control-label">DHW power</label>
        <Toggle value={dhwOn} ariaLabel="DHW power"
                onChange={(next) => editable && run(`DHW ${next ? "ON" : "OFF"}`, () => setTankPower(next))} />
        <span class="heating-control-state">{dhwOn ? "ON" : "OFF"}</span>
      </div>

      <div class="heating-control-row">
        <label class="heating-control-label">LWT offset</label>
        <Stepper value={lwt} unit="" step={0.5} disabled={!editable || busy}
                 onStep={(d) => setLwt((v) => clamp(Math.round((v + d) * 2) / 2, LWT_MIN, LWT_MAX))} />
        <button class="btn btn--sm" disabled={!editable || busy || lwt === dev?.lwt_offset}
                onClick={() => run(`LWT offset ${lwt >= 0 ? "+" : ""}${lwt}`, () => setLwtOffset(lwt))}>
          Apply
        </button>
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

// Onecta-style − value + stepper.
function Stepper({ value, unit = "°C", step = 1, disabled, onStep }: {
  value: number; unit?: string; step?: number; disabled?: boolean; onStep: (delta: number) => void;
}) {
  return (
    <div class="heating-stepper">
      <button type="button" class="heating-stepper-btn" disabled={disabled} aria-label="decrease"
              onClick={() => onStep(-step)}>−</button>
      <span class="heating-stepper-value">{value % 1 === 0 ? value : value.toFixed(1)}{unit}</span>
      <button type="button" class="heating-stepper-btn" disabled={disabled} aria-label="increase"
              onClick={() => onStep(step)}>+</button>
    </div>
  );
}
