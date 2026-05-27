import type { MetricsResponse } from "../../lib/types";
import { pct, pence } from "../../lib/format";
import { Spinner } from "../common/Spinner";
import "./efficiency.css";

interface EfficiencyWidgetProps {
  metrics: MetricsResponse | null;
  loading: boolean;
}

// Surfaces the LP performance KPIs from /metrics that were previously
// invisible: arbitrage efficiency, peak/off-peak import split, realised
// VWAP vs target VWAP. Operator-y but signals whether the system is
// "working" the tariff.
export function EfficiencyWidget({ metrics, loading }: EfficiencyWidgetProps) {
  if (loading && !metrics) return <Spinner label="Loading efficiency…" />;
  if (!metrics) return <div class="muted">No metrics.</div>;

  const arb = metrics.arbitrage_efficiency_pct;
  const peak = metrics.peak_import_pct;
  const offPeak = metrics.off_peak_import_pct;
  const realisedVwap = metrics.realised_vwap_pence;
  const targetVwap = metrics.target_vwap_pence;
  const slippage = metrics.slippage_pence;

  return (
    <div class="efficiency">
      {arb != null && (
        <Row
          label="Arbitrage efficiency"
          help="Battery cycle ROI — how much value the LP captured vs perfect"
          value={pct(arb, 0)}
          tone={arb >= 25 ? "ok" : arb >= 10 ? "warn" : "bad"}
        />
      )}
      {peak != null && offPeak != null && (
        <Row
          label="Peak / off-peak import"
          help="Lower peak% = more arbitrage win"
          value={`${peak.toFixed(0)}% / ${offPeak.toFixed(0)}%`}
          tone={peak < 15 ? "ok" : peak < 30 ? "warn" : "bad"}
        />
      )}
      {realisedVwap != null && (
        <Row
          label="Realised VWAP"
          help="Volume-weighted price you actually paid"
          value={pence(realisedVwap)}
          tone="neutral"
        />
      )}
      {targetVwap != null && slippage != null && (
        <Row
          label="Target VWAP / slippage"
          help="Slippage = realised − target. Lower is better."
          value={`${pence(targetVwap)} / +${slippage.toFixed(1)}p`}
          tone="neutral"
        />
      )}
    </div>
  );
}

function Row({ label, help, value, tone }: { label: string; help?: string; value: string; tone: "ok" | "warn" | "bad" | "neutral" }) {
  return (
    <div class={`eff-row eff-row--${tone}`}>
      <div class="eff-row-left">
        <div class="eff-row-label">{label}</div>
        {help && <div class="eff-row-help">{help}</div>}
      </div>
      <div class="eff-row-value">{value}</div>
    </div>
  );
}
