import { useState } from "preact/hooks";
import { simulateBatch, applyBatch } from "../../../lib/endpoints";
import { HemApiError } from "../../../lib/api";
import { role } from "../../../lib/auth";
import { toast } from "../../../lib/toast";
import { Modal } from "../../common/Modal";
import type { RecItem } from "../../../lib/recTypes";
import type { SimulateBatchResponse } from "../../../lib/types";
import { RecIcon } from "./RecIcon";
import { moneyText, moneyTone, showApply, sortItems, splitHeadline } from "./logic";

// "O que fazer": one row = who / one sentence / £ / a button ONLY when a setting
// changes. The Aplicar button runs the same simulate -> confirm -> apply flow as
// Settings (settings/batch/simulate, then settings/batch with X-Simulation-Id).

type Pending = { item: RecItem; sim: SimulateBatchResponse };

const detail = (e: unknown) =>
  e instanceof HemApiError ? (e.body || e.message) : e instanceof Error ? e.message : String(e);

export function ActionList({ items, onApplied }: { items: RecItem[]; onApplied?: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const isAdmin = role.value === "admin";

  const start = (item: RecItem) => {
    if (busy || !item.action) return;
    setBusy(item.key);
    simulateBatch({ [item.action.setting_key]: item.action.to })
      .then((sim) => setPending({ item, sim }))
      .catch((e) => toast.error("Não foi possível simular", detail(e)))
      .finally(() => setBusy(null));
  };

  const confirm = async () => {
    if (!pending || busy || !pending.item.action) return;
    const { item } = pending;
    const a = item.action!;
    setBusy(item.key);
    try {
      let sim = pending.sim;
      try {
        await applyBatch(sim.simulation_id, { [a.setting_key]: a.to });
      } catch (e) {
        // Simulation expired while the modal sat open: re-simulate once and retry.
        if (e instanceof HemApiError && (e.status === 409 || e.status === 410)
            && /Simulation(Expired|IdRequired|IdMismatch)/.test(e.body || "")) {
          sim = await simulateBatch({ [a.setting_key]: a.to });
          await applyBatch(sim.simulation_id, { [a.setting_key]: a.to });
        } else throw e;
      }
      toast.success(`${a.setting_key} = ${String(a.to)}`);
      setPending(null);
      onApplied?.();
    } catch (e) {
      toast.error("Não foi possível aplicar", detail(e));
    } finally {
      setBusy(null);
    }
  };

  if (items.length === 0) {
    return <p class="is-muted">Nada a fazer por enquanto — ainda juntando dados para as sugestões.</p>;
  }

  return (
    <>
      <ul class="is-list" aria-label="O que fazer">
        {sortItems(items).map((it) => {
          const { bold, rest } = splitHeadline(it.headline);
          const money = moneyText(it);
          const tone = moneyTone(it);
          return (
            <li class={`is-row is-row--${it.kind}`} key={it.key}>
              <div class="is-who"><RecIcon name={it.icon} /><span>{it.who}</span></div>
              <div class="is-body">
                <p class="is-head"><strong>{bold}</strong>{rest}</p>
                {it.detail && <p class="is-detail">{it.detail}</p>}
              </div>
              <div class="is-side">
                {money && <span class={`is-money is-money--${tone}`}>{money}</span>}
                {showApply(it, isAdmin) && (
                  <button type="button" class="btn btn--primary is-apply" disabled={busy !== null}
                          onClick={() => start(it)}>Aplicar</button>
                )}
              </div>
            </li>
          );
        })}
      </ul>
      <Modal
        open={!!pending}
        onClose={() => busy === null && setPending(null)}
        title="Aplicar sugestão"
        footer={
          <>
            <button type="button" class="btn btn--ghost" disabled={busy !== null} onClick={() => setPending(null)}>Cancelar</button>
            <button type="button" class="btn btn--primary" disabled={busy !== null} onClick={confirm}>Aplicar</button>
          </>
        }
      >
        {pending && (
          <p>{pending.sim.human_summary
            || `${pending.item.action?.setting_key}: ${String(pending.item.action?.from)} → ${String(pending.item.action?.to)}`}</p>
        )}
      </Modal>
    </>
  );
}
