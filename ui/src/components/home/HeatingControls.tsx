import { useEffect, useState } from "preact/hooks";
import type { DaikinDevice, DaikinOperationMode, ActionResult } from "../../lib/types";
import { setTankTemperature, setTankPower, setLwtOffset, setDaikinMode } from "../../lib/endpoints";
import { NumberInput, Select, Toggle } from "../common/Inputs";
import { Modal } from "../common/Modal";
import { toast } from "../../lib/toast";

interface HeatingControlsProps {
  dev: DaikinDevice | null;
  onChanged: () => void;
}

interface PendingAction {
  label: string;
  run: () => Promise<ActionResult>;
}

const MODES: DaikinOperationMode[] = ["heating", "cooling", "auto", "fan_only", "dry"];

// Manual Daikin controls (tank target / DHW power / LWT offset / mode). All
// writes are gated server-side by DAIKIN_CONTROL_MODE=active; when passive the
// panel renders disabled with an explanation. Each write is confirmed in a
// modal first, then sent with skip_confirmation:true.
export function HeatingControls({ dev, onChanged }: HeatingControlsProps) {
  const active = dev?.control_mode === "active";
  const [tankTarget, setTankTarget] = useState<number>(dev?.tank_target ?? 45);
  const [lwt, setLwt] = useState<number>(dev?.lwt_offset ?? 0);
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [busy, setBusy] = useState(false);

  // Re-sync the editable fields when the device's confirmed values change
  // (e.g. after a successful write refreshes status, or the optimizer writes
  // externally). Daikin status is fetch-once (not polled), so this never
  // clobbers mid-typing during normal use.
  useEffect(() => {
    if (dev?.tank_target != null) setTankTarget(dev.tank_target);
  }, [dev?.tank_target]);
  useEffect(() => {
    if (dev?.lwt_offset != null) setLwt(dev.lwt_offset);
  }, [dev?.lwt_offset]);

  const confirm = (label: string, run: () => Promise<ActionResult>) => setPending({ label, run });

  const runPending = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      const res = await pending.run();
      toast.success(res.message || "Done");
      onChanged();
    } catch (e) {
      toast.error("Daikin command failed", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setPending(null);
    }
  };

  const dhwOn = dev?.tank_power ?? false;
  const mode = (dev?.mode as DaikinOperationMode) || "heating";
  // If the device reports a mode outside the known union, surface it as an
  // extra option so the Select shows the real state instead of a blank.
  const modeOptions: DaikinOperationMode[] = MODES.includes(mode) ? MODES : [...MODES, mode];

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

      <div class="heating-control-row">
        <label class="heating-control-label">Tank target</label>
        <NumberInput value={tankTarget} onChange={setTankTarget} min={30} max={65} step={1} ariaLabel="Tank target °C" />
        <button class="btn btn--sm" disabled={!active || busy}
                onClick={() => confirm(`Set DHW tank target to ${tankTarget}°C?`, () => setTankTemperature(tankTarget))}>
          Set
        </button>
      </div>

      <div class="heating-control-row">
        <label class="heating-control-label">DHW power</label>
        <Toggle value={dhwOn} ariaLabel="DHW power"
                onChange={(next) => active && confirm(`Turn DHW tank ${next ? "ON" : "OFF"}?`, () => setTankPower(next))} />
        <span class="heating-control-state">{dhwOn ? "ON" : "OFF"}</span>
      </div>

      <div class="heating-control-row">
        <label class="heating-control-label">LWT offset</label>
        <NumberInput value={lwt} onChange={setLwt} min={-10} max={10} step={0.5} ariaLabel="LWT offset" />
        <button class="btn btn--sm" disabled={!active || busy}
                onClick={() => confirm(`Set leaving-water offset to ${lwt >= 0 ? "+" : ""}${lwt}?`, () => setLwtOffset(lwt))}>
          Set
        </button>
      </div>

      <div class="heating-control-row">
        <label class="heating-control-label">Mode</label>
        <Select<DaikinOperationMode> value={mode} options={modeOptions} ariaLabel="Operation mode"
                onChange={(m) => active && m !== mode && confirm(`Set Daikin mode to ${m}?`, () => setDaikinMode(m))} />
      </div>

      <Modal open={pending != null} onClose={() => !busy && setPending(null)} title="Confirm Daikin command" width="sm"
             footer={
               <>
                 <button class="btn btn--ghost" disabled={busy} onClick={() => setPending(null)}>Cancel</button>
                 <button class="btn btn--primary" disabled={busy} onClick={runPending}>{busy ? "Sending…" : "Confirm"}</button>
               </>
             }>
        <p>{pending?.label}</p>
        <p class="muted heating-controls-hint">This writes directly to the heat pump via Onecta.</p>
      </Modal>
    </div>
  );
}
