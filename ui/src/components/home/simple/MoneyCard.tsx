import { Link } from "wouter-preact";
import type { PlanFrontsBilling, PlanFrontsResponse } from "../../../lib/types";
import { gbp, fmt, spendVerdict, spendLine, todayNet, cycleLabel, billingVerdict, type HolidayFlags } from "./logic";
import { BandStrip } from "./BandStrip";

export function MoneyCard({ fronts, nowMs, holiday, tz }: {
  fronts: PlanFrontsResponse | null; nowMs: number; holiday: HolidayFlags; tz?: string;
}) {
  const spend = fronts?.spend && !fronts.spend.error ? fronts.spend : null;
  const billing = fronts?.billing && !fronts.billing.error ? fronts.billing : null;
  const v = spendVerdict(spend);
  const today = todayNet(spend);
  const approx = spend?.score_basis === "forecast";
  return (
    <section class="hs-card hs-money" aria-label="Dinheiro">
      <div class="hs-money-grid">
        <div class="hs-money-col">
          <h2 class="hs-eyebrow">Hoje</h2>
          <div class="hs-big">
            <span class="hs-num hs-num--md">{today != null ? (today < 0 ? "−" : "") + gbp(today) : "—"}</span>
            {v.word && <span class={`hs-verdict hs-verdict--${v.tone}`}>{approx ? "previsto: " : ""}{v.word}</span>}
          </div>
          <p class="hs-meaning">{spendLine(spend)}</p>
        </div>
        <div class="hs-money-col">
          <h2 class="hs-eyebrow">Conta Octopus{billing ? ` · ciclo ${cycleLabel(billing)}` : ""}</h2>
          {billing ? (
            <>
              <div class="hs-big">
                <span class="hs-num hs-num--md">{gbp(billing.realised_gbp)}</span>
                <span class="hs-unit hs-unit--sm">
                  {billing.forecast_cycle_gbp != null ? `de ~£${fmt(billing.forecast_cycle_gbp, 0)}` : "até agora"}
                </span>
              </div>
              <BillingVerdict b={billing} />
            </>
          ) : (
            <p class="hs-meaning">Sem dados do ciclo ainda.</p>
          )}
        </div>
      </div>
      {fronts && <BandStrip fronts={fronts} nowMs={nowMs} holiday={holiday} tz={tz} />}
    </section>
  );
}

function BillingVerdict({ b }: { b: PlanFrontsBilling }) {
  const v = billingVerdict(b);
  if (!v) return null;
  return (
    <p class="hs-meaning">
      <span class={v.good ? "hs-good" : "hs-warn"}>{v.lead}</span>
      {v.others.length > 0 && <> · no ciclo: {v.others.map((o, i) => (
        <span key={i} class="hs-nowrap">{i > 0 ? " · " : ""}{o}</span>
      ))}</>}{" "}
      <Link href="/insights" class="hs-link">ver →</Link>
    </p>
  );
}
