import { useState } from "preact/hooks";
import { usePoll } from "../../../lib/poll";
import {
  getDhwBoost, simulateDhwBoost, applyDhwBoost, simulateDhwBoostUndo, applyDhwBoostUndo,
} from "../../../lib/endpoints";
import { HemApiError } from "../../../lib/api";
import { toast } from "../../../lib/toast";
import { role } from "../../../lib/auth";
import type { ActionDiffResponse, CockpitNow, PlanFrontsResponse } from "../../../lib/types";
import { Icon } from "../../common/Icon";
import { aguaTile, holidayFlags } from "../simple/logic.ts";
import { barPct, waterHead, afterRow, legionellaRow, boostView, confirmCopy, hhmm } from "./water.ts";
import "../../../styles/home-v3-water.css";

const MOCK = import.meta.env.DEV && new URLSearchParams(location.search).get("mock") === "1";

const isSimIdError = (e: unknown): boolean =>
  e instanceof HemApiError && (e.status === 409 || e.status === 410) &&
  /Simulation(Expired|IdRequired|IdMismatch)/.test(e.body || "");

// Home v3 "Água quente" (#901): tank now, bar with the target marker, three
// rows (next shower, next step, next legionella) and, for the admin, "Banho
// extra hoje" (simulate -> confirm -> apply, with undo). Every value is read
// from /cockpit/now, /plan/fronts or /dhw/boost; a row without data is dropped.
export function WaterCard({ now, fronts, nowMs, tz, onChanged }: {
  now: CockpitNow;
  fronts: PlanFrontsResponse | null;
  nowMs: number;
  tz?: string;
  onChanged: () => void;
}) {
  const boost = usePoll(async () => {
    if (import.meta.env.DEV && MOCK) return (await import("../../../lib/mock/dhwBoost")).mockDhwBoost();
    return getDhwBoost();
  }, 60_000);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<ActionDiffResponse | null>(null);
  const [confirmUndo, setConfirmUndo] = useState(false);

  const tk = fronts?.tank && !fronts.tank.error ? fronts.tank : null;
  const off = holidayFlags(now).tank;
  const st = boost.data;
  const tile = aguaTile(now, fronts);
  const tankNow = now.state.tank_c ?? tk?.tank_now_c ?? null;
  const head = waterHead(tk, tankNow, nowMs, {
    holidayOff: off,
    boostUntil: st?.active ? st.until_utc : null,
    boostTarget: st?.active ? st.boost_target_c : null,
    differential: st?.reheat_differential_c ?? null,
  });
  const untilText = st?.until_utc ? hhmm(st.until_utc, tz) : null;
  const after = afterRow(tile.next, st?.active ? { until: untilText, target: st.boost_target_c } : null);
  const legio = legionellaRow(st?.legionella, nowMs, tz);
  const fill = barPct(tankNow);
  const mark = barPct(head.target);

  const passive = now.modes?.daikin_control_mode === "passive";
  const view = role.value === "admin" && !passive && !off ? boostView(st, tankNow, tz) : { kind: "hidden" as const };

  const run = async (fn: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    try { await fn(); } catch (e) {
      const detail = e instanceof HemApiError ? (e.body || e.message) : e instanceof Error ? e.message : String(e);
      toast.error("Não foi possível aplicar", detail);
    } finally { setBusy(false); }
  };
  const refreshAll = () => { void boost.refresh(); onChanged(); };

  const ask = () => void run(async () => {
    if (import.meta.env.DEV && MOCK) {
      setConfirm({ simulation_id: "mock" } as ActionDiffResponse);
      return;
    }
    setConfirm(await simulateDhwBoost());
  });
  const apply = () => void run(async () => {
    if (!confirm) return;
    if (import.meta.env.DEV && MOCK) {
      (await import("../../../lib/mock/dhwBoost")).mockDhwBoostSet(true);
    } else {
      try { await applyDhwBoost(confirm.simulation_id); } catch (e) {
        if (isSimIdError(e)) {   // sat past the 5 min TTL: re-simulate, ask again
          setConfirm(await simulateDhwBoost());
          toast.error("Simulação expirada", "Confira o resumo e confirme de novo.");
          return;
        }
        throw e;
      }
    }
    toast.success("Banho extra pedido");
    setConfirm(null);
    refreshAll();
  });
  const undo = () => void run(async () => {
    let restored = true;
    if (import.meta.env.DEV && MOCK) {
      (await import("../../../lib/mock/dhwBoost")).mockDhwBoostSet(false);
    } else {
      const sim = await simulateDhwBoostUndo();
      restored = (await applyDhwBoostUndo(sim.simulation_id)).restored;
    }
    setConfirmUndo(false);
    if (restored) toast.success("Banho extra desfeito");
    else toast.info("Banho extra encerrado", "O tanque volta ao plano assim que o Daikin puder receber o comando.");
    refreshAll();
  });

  return (
    <section class="hs-tile hw" aria-label="Água quente">
      <h2 class="hs-eyebrow"><Icon name="droplet" size={14} />Água quente</h2>
      <div class="hw-head">
        <div class="hs-big">
          <span class="hs-num">{tankNow != null ? String(Math.round(tankNow)) : "—"}</span>
          {tankNow != null && <span class="hs-unit">°C</span>}
        </div>
        {head.text && <span class={`hw-state${head.state === "off" ? " hw-state--off" : ""}`}>{head.text}</span>}
      </div>

      <div>
        <div class="hw-bar" role="img"
             aria-label={head.target != null ? "Temperatura do tanque, com a meta marcada" : "Temperatura do tanque"}>
          {fill != null && <div class="hw-fill" style={{ width: `${fill}%` }} />}
          {mark != null && <div class="hw-mark" style={{ left: `${mark}%` }} />}
        </div>
        <div class="hw-axis"><span>30 °C</span><span>60 °C</span></div>
      </div>

      <dl class="hw-rows">
        <dt>Banho</dt>
        <dd class={tile.tone === "ok" ? "is-ok" : tile.tone === "warn" ? "is-warn" : ""}>{tile.meaning}</dd>
        {after && <><dt>Depois</dt><dd>{after}</dd></>}
        {legio && <><dt>Legionela</dt><dd>{legio}</dd></>}
      </dl>

      {view.kind !== "hidden" && (
        <div class="hw-boost">
          {view.kind === "idle" && !confirm && (
            view.hint
              ? <p class="hw-hint">{view.hint}</p>
              : <button type="button" class="btn" disabled={busy} onClick={ask}>Banho extra hoje</button>
          )}
          {view.kind === "idle" && confirm && st && (
            <div class="hw-confirm">
              <span>{confirmCopy(st, hhmm(new Date(nowMs + st.hold_hours * 3_600_000).toISOString(), tz), now.current_slot?.kind)}</span>
              <div class="hw-actions">
                <button type="button" class="btn btn--sm btn--primary" disabled={busy} onClick={apply}>{busy ? "Aplicando…" : "Confirmar"}</button>
                <button type="button" class="btn btn--sm" disabled={busy} onClick={() => setConfirm(null)}>Cancelar</button>
              </div>
            </div>
          )}
          {view.kind === "active" && (
            confirmUndo ? (
              <div class="hw-confirm">
                <span>Devolve o tanque ao plano agora e desliga o Powerful.</span>
                <div class="hw-actions">
                  <button type="button" class="btn btn--sm btn--primary" disabled={busy} onClick={undo}>{busy ? "Desfazendo…" : "Confirmar"}</button>
                  <button type="button" class="btn btn--sm" disabled={busy} onClick={() => setConfirmUndo(false)}>Cancelar</button>
                </div>
              </div>
            ) : (
              <p class="hw-note">
                Banho extra ativo{view.until ? `: o plano espera até ${view.until}` : ""}.{" "}
                <button type="button" class="btn btn--sm" disabled={busy} onClick={() => setConfirmUndo(true)}>Desfazer</button>
              </p>
            )
          )}
        </div>
      )}
    </section>
  );
}
