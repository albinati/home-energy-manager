import { useState } from "preact/hooks";
import { useFetch } from "../../lib/poll";
import { getTuningSuggestions, simulateBatch, applyBatch, type TuningSuggestion } from "../../lib/endpoints";
import { HemApiError } from "../../lib/api";
import { role } from "../../lib/auth";
import { toast } from "../../lib/toast";
import { Modal } from "../common/Modal";
import { Spinner } from "../common/Spinner";
import type { SimulateBatchResponse } from "../../lib/types";

// Weekly fine-tuning review (#832). SUGGESTIONS ONLY: nothing here is applied
// until an admin presses Apply, which runs the same simulate -> confirm ->
// apply flow as the Settings page (settings/batch with X-Simulation-Id).

const VERDICT_LABEL: Record<string, string> = {
  recommended: "Recommended",
  "trade-off": "Trade-off",
  "comfort-first": "Comfort first",
};

function fmtPence(p: number | null): string {
  if (p == null) return "-";
  const sign = p > 0 ? "+" : p < 0 ? "-" : "";
  return `${sign}${Math.abs(p).toFixed(0)} p/wk`;
}
function fmtHours(h: number | null): string {
  if (h == null) return "-";
  const sign = h > 0 ? "+" : h < 0 ? "-" : "";
  return `${sign}${Math.abs(h).toFixed(1)} h`;
}

type Pending = { row: TuningSuggestion; value: unknown; sim: SimulateBatchResponse };

export function TuningSuggestionsCard() {
  const res = useFetch(() => getTuningSuggestions(4), [], { cacheKey: "tuning:4", track: true });
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const rows = res.data?.suggestions ?? [];
  const latestWeek = rows[0]?.week_start;
  const latest = rows.filter((r) => r.week_start === latestWeek);
  const older = rows.filter((r) => r.week_start !== latestWeek);
  const p0 = latest[0]?.payload;
  const ctx = p0?.context;
  const ctxLine = ctx
    ? `evaluated with W3 ${ctx.w3_active ? "on" : "off"} · ${ctx.control_mode || "?"} · ${ctx.tariff_banded ? "banded" : "dynamic"}`
    : null;
  const partial = p0?.status === "partial";

  const detail = (e: unknown) =>
    e instanceof HemApiError ? (e.body || e.message) : e instanceof Error ? e.message : String(e);

  const start = (row: TuningSuggestion) => {
    if (busy) return;
    const value = row.payload?.body?.value ?? row.suggested_value;
    setBusy(true);
    simulateBatch({ [row.key]: value })
      .then((sim) => setPending({ row, value, sim }))
      .catch((e) => toast.error("Simulate failed", detail(e)))
      .finally(() => setBusy(false));
  };

  const confirm = async () => {
    if (!pending || busy) return;
    setBusy(true);
    const { row, value } = pending;
    try {
      let sim = pending.sim;
      try {
        await applyBatch(sim.simulation_id, { [row.key]: value });
      } catch (e) {
        // Sim-id expired while the modal sat open: re-simulate once and retry.
        if (e instanceof HemApiError && (e.status === 409 || e.status === 410)
            && /Simulation(Expired|IdRequired|IdMismatch)/.test(e.body || "")) {
          sim = await simulateBatch({ [row.key]: value });
          await applyBatch(sim.simulation_id, { [row.key]: value });
        } else throw e;
      }
      toast.success(`${row.key} set to ${String(value)}`);
      setPending(null);
    } catch (e) {
      toast.error("Apply failed", detail(e));
    } finally {
      setBusy(false);
    }
  };

  const isAdmin = role.value === "admin";

  const table = (list: TuningSuggestion[]) => (
    <div class="insights-table-wrap">
      <table class="insights-table">
        <thead>
          <tr>
            <th>Setting</th>
            <th class="num">Now</th>
            <th class="num">Suggested</th>
            <th class="num">Cost</th>
            <th class="num">Comfort</th>
            <th>Verdict</th>
            {isAdmin && <th />}
          </tr>
        </thead>
        <tbody>
          {list.map((r) => (
            <tr key={r.id}>
              <td>{r.key}</td>
              <td class="num">{r.current_value}</td>
              <td class="num">{r.suggested_value}</td>
              <td class={`num${(r.delta_pence_per_week ?? 0) < 0 ? " insights-cheaper" : ""}`}>
                {fmtPence(r.delta_pence_per_week)}
              </td>
              <td class="num">{fmtHours(r.delta_comfort_hours)}</td>
              <td>{VERDICT_LABEL[r.verdict] ?? r.verdict}</td>
              {isAdmin && (
                <td>
                  <button type="button" class="btn btn--ghost" disabled={busy} onClick={() => start(r)}>Apply</button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );

  return (
    <section class={`insights-card${res.loading && res.data ? " is-updating" : ""}`}>
      <header class="load-pattern-head">
        <h2>Weekly tuning suggestions</h2>
        <p class="muted">
          Each Sunday the last 7 days are replayed with every comfort / cost setting one
          step either side. Cost is the replayed plan under actual prices; comfort is hours
          the predicted house sits below the night / peak floors. Nothing is applied
          automatically.
        </p>
      </header>
      {res.loading && !res.data && <Spinner label="Loading suggestions" />}
      {res.error && <p class="insights-error">Couldn't load suggestions: {res.error.message}</p>}
      {res.data && latest.length === 0 && (
        <p class="muted">No suggestions yet. The first review runs on the next Sunday morning.</p>
      )}
      {latest.length > 0 && (
        <>
          <p class="muted">
            Week of {latestWeek}
            {partial && " · partial (time budget reached, some settings were not evaluated)"}
            {ctxLine && ` · ${ctxLine}`}
          </p>
          {table(latest)}
        </>
      )}
      {older.length > 0 && (
        <details>
          <summary class="muted">Earlier weeks</summary>
          {table(older)}
        </details>
      )}
      <Modal
        open={!!pending}
        onClose={() => !busy && setPending(null)}
        title="Apply suggestion"
        footer={
          <>
            <button type="button" class="btn btn--ghost" disabled={busy} onClick={() => setPending(null)}>Cancel</button>
            <button type="button" class="btn btn--primary" disabled={busy} onClick={confirm}>Apply</button>
          </>
        }
      >
        {pending && (
          <p>{pending.sim.human_summary || `${pending.row.key}: ${pending.row.current_value} to ${String(pending.value)}`}</p>
        )}
      </Modal>
    </section>
  );
}
