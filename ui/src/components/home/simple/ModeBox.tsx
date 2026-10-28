import { useEffect, useState } from "preact/hooks";
import {
  simulateBatch, applyBatch, simulateProposeOptimization, proposeOptimization,
} from "../../../lib/endpoints";
import { HemApiError } from "../../../lib/api";
import { Modal } from "../../common/Modal";
import { toast } from "../../../lib/toast";
import type { ActionDiffResponse, CockpitVacation, PlanFrontsBattery, PlanFrontsTariff, SimulateBatchResponse } from "../../../lib/types";
import { toDatetimeLocal, isAwayPreset, vacationHelp } from "./logic";

// "Modo da casa" (admin only): Normal | Férias até <datetime>, plus "Replanejar
// agora". Every write is simulate -> confirm -> apply (X-Simulation-Id); the
// vacation batch carries VACATION_RETURN_AT BEFORE OPTIMIZATION_PRESET (#873).

type Confirm =
  | { kind: "mode"; changes: Record<string, unknown>; sim: SimulateBatchResponse; label: string }
  | { kind: "replan"; diff: ActionDiffResponse };

const isSimIdError = (e: unknown): boolean =>
  e instanceof HemApiError && (e.status === 409 || e.status === 410) &&
  /Simulation(Expired|IdRequired|IdMismatch)/.test(e.body || "");

export function ModeBox({ vacation, preset, battery, tariff, nowMs, tz, onChanged }: {
  vacation: CockpitVacation | null | undefined;
  preset: string | undefined;
  battery: PlanFrontsBattery | null | undefined;
  tariff: PlanFrontsTariff | null | undefined;
  nowMs: number;
  tz?: string;
  onChanged: () => void;
}) {
  const away = !!vacation?.active || isAwayPreset(preset);
  const serverRet = toDatetimeLocal(vacation?.return_at);
  const [sel, setSel] = useState<"normal" | "vacation">(away ? "vacation" : "normal");
  const [ret, setRet] = useState<string>(serverRet);
  // Re-seed when the SERVER state changes (apply, another admin, recovery job);
  // never while the user is mid-edit (draft differs from the last server value).
  const [seed, setSeed] = useState({ away, serverRet });
  useEffect(() => {
    if (seed.away === away && seed.serverRet === serverRet) return;
    setSeed({ away, serverRet });
    setSel(away ? "vacation" : "normal");
    setRet(serverRet);
  }, [away, serverRet]);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<Confirm | null>(null);

  const guard = async (fn: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    try { await fn(); } catch (e) {
      const detail = e instanceof HemApiError ? (e.body || e.message) : e instanceof Error ? e.message : String(e);
      toast.error("Não foi possível aplicar", detail);
    } finally { setBusy(false); }
  };

  const changesFor = (m: "normal" | "vacation"): Record<string, unknown> =>
    m === "vacation"
      ? { VACATION_RETURN_AT: ret, OPTIMIZATION_PRESET: "vacation" }   // order matters (#873)
      : { OPTIMIZATION_PRESET: "normal" };

  const simulateMode = () => void guard(async () => {
    const changes = changesFor(sel);
    setConfirm({ kind: "mode", changes, sim: await simulateBatch(changes), label: sel === "vacation" ? "Férias" : "Normal" });
  });
  const simulateReplan = () => void guard(async () => {
    setConfirm({ kind: "replan", diff: await simulateProposeOptimization() });
  });

  const apply = (c: Confirm) => void guard(async () => {
    try {
      if (c.kind === "mode") await applyBatch(c.sim.simulation_id, c.changes);
      else await proposeOptimization(c.diff.simulation_id);
    } catch (e) {
      if (isSimIdError(e)) {   // sat past the 5 min TTL: re-simulate once, ask again
        setConfirm(c.kind === "mode"
          ? { ...c, sim: await simulateBatch(c.changes) }
          : { kind: "replan", diff: await simulateProposeOptimization() });
        toast.error("Simulação expirada", "Confira o resumo atualizado e confirme de novo.");
        return;
      }
      throw e;
    }
    toast.success(c.kind === "mode" ? `Modo da casa: ${c.label}` : "Replanejamento iniciado");
    setConfirm(null);
    onChanged();
  });

  const dirty = sel !== (away ? "vacation" : "normal") || (sel === "vacation" && serverRet !== ret);
  const canApply = dirty && (sel === "normal" || !!ret) && !busy;

  return (
    <section class="hs-card hs-mode" aria-label="Modo da casa">
      <div class="hs-mode-row">
        <h2 class="hs-eyebrow">Modo da casa</h2>
        <div class="hs-seg-ctl" role="radiogroup" aria-label="Modo da casa">
          {([["normal", "Normal"], ["vacation", "Férias até…"]] as const).map(([k, label]) => (
            <button key={k} type="button" role="radio" aria-checked={sel === k}
                    class={`hs-seg-btn${sel === k ? " is-active" : ""}`} onClick={() => setSel(k)}>{label}</button>
          ))}
        </div>
        {sel === "vacation" && (
          <label class="hs-ret">
            volta
            <input type="datetime-local" value={ret}
                   onInput={(e) => setRet((e.currentTarget as HTMLInputElement).value)} />
          </label>
        )}
        {dirty && (
          <button type="button" class="btn btn--sm btn--primary" disabled={!canApply} onClick={simulateMode}>Aplicar</button>
        )}
        <span class="grow" />
        <button type="button" class="btn btn--sm" disabled={busy} onClick={simulateReplan}>Replanejar agora</button>
      </div>
      <p class="hs-help">{vacationHelp(vacation, battery, tariff, nowMs, tz)}</p>

      <Modal
        open={confirm != null}
        onClose={() => setConfirm(null)}
        width="sm"
        title={confirm?.kind === "replan" ? "Replanejar agora?" : "Mudar o modo da casa?"}
        footer={confirm && (
          <>
            <button class="btn btn--ghost" disabled={busy} onClick={() => setConfirm(null)}>Cancelar</button>
            <button class="btn btn--primary" disabled={busy} onClick={() => apply(confirm)}>
              {busy ? "Aplicando…" : "Confirmar"}
            </button>
          </>
        )}
      >
        {confirm?.kind === "mode" && (
          <>
            <p>{confirm.sim.human_summary || "Sem resumo disponível."}</p>
            {confirm.sim.safety_flags.length > 0 && <p class="hs-warn">{confirm.sim.safety_flags.join(" · ")}</p>}
          </>
        )}
        {confirm?.kind === "replan" && (
          <>
            <p>{confirm.diff.human_summary || "Sem resumo disponível."}</p>
            {confirm.diff.safety_flags.length > 0 && <p class="hs-warn">{confirm.diff.safety_flags.join(" · ")}</p>}
          </>
        )}
      </Modal>
    </section>
  );
}
