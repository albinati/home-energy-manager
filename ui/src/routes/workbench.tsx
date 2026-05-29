import { useState } from "preact/hooks";
import { useFetch } from "../lib/poll";
import {
  getWorkbenchSchema,
  simulateWorkbench,
  promoteSimulateWorkbench,
  promoteWorkbench,
} from "../lib/endpoints";
import type { WorkbenchField, WorkbenchSimulateResponse, WorkbenchPromoteDiff } from "../lib/types";
import { Spinner } from "../components/common/Spinner";
import { NumberInput, Select } from "../components/common/Inputs";
import { Modal } from "../components/common/Modal";
import { Pill } from "../components/common/Pill";
import { WorkbenchPlanChart } from "../components/workbench/WorkbenchPlanChart";
import { toast } from "../lib/toast";
import { gbp } from "../lib/format";
import "../components/workbench/workbench.css";

type OverrideVal = number | string;

// Workbench — tune LP knobs, simulate the plan, promote the promotable subset
// to prod (runtime_settings). Mirrors the backend /workbench/* flow. The load
// knob (LP_LOAD_SCALE_FACTOR) is surfaced in its own group at the top.
export default function Workbench() {
  const schema = useFetch(getWorkbenchSchema, []);
  const [overrides, setOverrides] = useState<Record<string, OverrideVal>>({});
  const [sim, setSim] = useState<WorkbenchSimulateResponse | null>(null);
  const [simBusy, setSimBusy] = useState(false);
  const [diff, setDiff] = useState<WorkbenchPromoteDiff | null>(null);
  const [promoteBusy, setPromoteBusy] = useState(false);
  const [profileName, setProfileName] = useState("");

  const dirty = Object.keys(overrides).length > 0;

  const setVal = (key: string, v: OverrideVal) => setOverrides((o) => ({ ...o, [key]: v }));
  const reset = () => { setOverrides({}); setSim(null); };

  const runSimulate = async () => {
    setSimBusy(true);
    try {
      setSim(await simulateWorkbench(overrides));
    } catch (e) {
      toast.error("Simulation failed", e instanceof Error ? e.message : String(e));
    } finally {
      setSimBusy(false);
    }
  };

  const openPromote = async () => {
    setPromoteBusy(true);
    try {
      setDiff(await promoteSimulateWorkbench(overrides));
    } catch (e) {
      toast.error("Nothing to promote", e instanceof Error ? e.message : String(e));
    } finally {
      setPromoteBusy(false);
    }
  };

  const confirmPromote = async () => {
    if (!diff) return;
    setPromoteBusy(true);
    try {
      const res = await promoteWorkbench(diff.simulation_id, overrides, profileName.trim() || undefined);
      const okCount = res.promoted.filter((p) => p.ok).length;
      toast.success(`Promoted ${okCount} setting${okCount === 1 ? "" : "s"} to prod`);
      setDiff(null);
      setProfileName("");
      reset();
      schema.refresh();
    } catch (e) {
      toast.error("Promote failed", e instanceof Error ? e.message : String(e));
    } finally {
      setPromoteBusy(false);
    }
  };

  if (schema.loading && !schema.data) {
    return <div class="page-padded"><Spinner label="Loading workbench…" /></div>;
  }
  if (!schema.data) {
    return (
      <div class="page-padded">
        <p class="muted">Workbench unavailable: {schema.error?.message || "no data"}</p>
        <button class="btn" onClick={() => schema.refresh()}>Retry</button>
      </div>
    );
  }

  const fields = schema.data.fields;
  // Render the load group first (the headline knob), then the rest in order.
  const groupOrder = ["load", ...schema.data.groups.filter((g) => g !== "load")];
  const byGroup = new Map<string, WorkbenchField[]>();
  for (const f of fields) {
    if (!byGroup.has(f.group)) byGroup.set(f.group, []);
    byGroup.get(f.group)!.push(f);
  }

  return (
    <div class="page-padded wb">
      <header class="wb-head">
        <h1>Workbench</h1>
        <p class="muted">
          Tune the LP planner, simulate the resulting plan, and promote the promotable
          subset to production. Simulation never writes to hardware or the cloud.
        </p>
      </header>

      {groupOrder.map((g) => {
        const gf = byGroup.get(g);
        if (!gf || !gf.length) return null;
        return (
          <section key={g} class={`wb-group${g === "load" ? " wb-group--load" : ""}`}>
            <h2 class="wb-group-title">{g}</h2>
            <div class="wb-fields">
              {gf.map((f) => (
                <FieldRow key={f.key} field={f}
                          value={overrides[f.key] ?? (f.current as OverrideVal)}
                          edited={overrides[f.key] !== undefined}
                          onChange={(v) => setVal(f.key, v)} />
              ))}
            </div>
          </section>
        );
      })}

      <div class="wb-bar">
        <span class="wb-bar-status">{dirty ? `${Object.keys(overrides).length} change(s)` : "no changes"}</span>
        <div class="wb-bar-actions">
          {dirty && <button class="btn btn--ghost" onClick={reset} disabled={simBusy || promoteBusy}>Reset</button>}
          <button class="btn" onClick={runSimulate} disabled={!dirty || simBusy}>
            {simBusy ? "Simulating…" : "Simulate"}
          </button>
          <button class="btn btn--primary" onClick={openPromote} disabled={!dirty || promoteBusy}>
            Promote to prod…
          </button>
        </div>
      </div>

      {sim && <SimResult sim={sim} />}

      <Modal open={diff != null} onClose={() => !promoteBusy && setDiff(null)} title="Promote to production" width="md"
             footer={
               <>
                 <button class="btn btn--ghost" disabled={promoteBusy} onClick={() => setDiff(null)}>Cancel</button>
                 <button class="btn btn--primary" disabled={promoteBusy} onClick={confirmPromote}>
                   {promoteBusy ? "Promoting…" : "Confirm promote"}
                 </button>
               </>
             }>
        <p>{diff?.human_summary || "Promote the promotable overrides to runtime settings."}</p>
        {diff?.non_promotable_overrides && Object.keys(diff.non_promotable_overrides).length > 0 && (
          <p class="muted wb-nonprom">
            Not promotable (simulation-only): {Object.keys(diff.non_promotable_overrides).join(", ")}
          </p>
        )}
        <label class="wb-profile">
          <span>Save as profile (optional)</span>
          <input class="input" type="text" value={profileName} placeholder="e.g. away-week"
                 onInput={(e) => setProfileName((e.currentTarget as HTMLInputElement).value)} />
        </label>
      </Modal>
    </div>
  );
}

function FieldRow({ field, value, edited, onChange }: {
  field: WorkbenchField; value: OverrideVal; edited: boolean; onChange: (v: OverrideVal) => void;
}) {
  return (
    <div class={`wb-field${edited ? " wb-field--edited" : ""}`}>
      <div class="wb-field-head">
        <code class="wb-field-key">{field.key}</code>
        {field.promotable
          ? <Pill tone="ok" title="Has a runtime_settings entry — can be promoted to prod">promotable</Pill>
          : <Pill tone="dim" title="Simulation-only — no prod equivalent">sim-only</Pill>}
        {edited && <Pill tone="warn">edited</Pill>}
      </div>
      <p class="wb-field-desc muted">{field.description}</p>
      <div class="wb-field-input">
        {field.enum
          ? <Select value={String(value)} options={field.enum} onChange={(v) => onChange(v)} ariaLabel={field.key} />
          : <NumberInput value={value} onChange={(n) => onChange(n)} min={field.min} max={field.max}
                         step={field.type === "int" ? 1 : 0.1} ariaLabel={field.key} />}
      </div>
    </div>
  );
}

function SimResult({ sim }: { sim: WorkbenchSimulateResponse }) {
  if (!sim.ok) {
    return <div class="wb-sim wb-sim--err"><strong>Simulation failed</strong><p class="muted">{sim.error || sim.status}</p></div>;
  }
  const obj = sim.objective_pence != null ? gbp(sim.objective_pence / 100) : "—";
  const applied = Object.keys(sim.applied_overrides || {});
  const ignored = Object.keys(sim.ignored_overrides || {});
  return (
    <div class="wb-sim">
      <div class="wb-sim-head">
        <span>Simulated plan</span>
        <Pill tone={sim.status === "Optimal" ? "ok" : "warn"}>{sim.status}</Pill>
      </div>
      <div class="wb-sim-stats">
        <Stat label="Objective" value={obj} hint="LP objective cost over the horizon" />
        <Stat label="Mean Agile" value={sim.actual_mean_agile_pence != null ? `${sim.actual_mean_agile_pence.toFixed(1)}p` : "—"} />
        <Stat label="Horizon solar" value={sim.forecast_solar_kwh_horizon != null ? `${sim.forecast_solar_kwh_horizon.toFixed(1)} kWh` : "—"} />
        <Stat label="Mean load/slot" value={sim.mu_load_kwh_per_slot != null ? `${sim.mu_load_kwh_per_slot.toFixed(2)} kWh` : "—"} />
        <Stat label="Slots" value={String(sim.slot_count ?? "—")} />
      </div>
      {sim.slots && sim.slots.length > 0 && <WorkbenchPlanChart slots={sim.slots} />}
      {applied.length > 0 && <p class="wb-sim-applied">Applied: <code>{applied.join(", ")}</code></p>}
      {ignored.length > 0 && <p class="muted">Ignored: {ignored.join(", ")}</p>}
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div class="wb-stat" title={hint}>
      <div class="wb-stat-value">{value}</div>
      <div class="wb-stat-label">{label}</div>
    </div>
  );
}
