import type { MetricsResponse } from "../../lib/types";
import { pct, pence } from "../../lib/format";
import { Spinner } from "../common/Spinner";
import "./efficiency.css";

interface EfficiencyWidgetProps {
  metrics: MetricsResponse | null;
  loading: boolean;
}

// When today's imports are below this threshold, the import-only KPIs
// (arbitrage %, VWAP slippage, off-peak share) become statistically
// uninformative — a single half-hour blip at a peak price moves them
// drastically. Surface "Self-use day" interpretation instead of red flags.
const SELF_USE_DAY_IMPORT_KWH_THRESHOLD = 3.0;

// Operator-facing KPIs from /metrics with inline benchmarks so each number
// reads as "good / ok / needs attention" without you needing to remember
// what good looks like.
export function EfficiencyWidget({ metrics, loading }: EfficiencyWidgetProps) {
  if (loading && !metrics) return <Spinner label="Loading efficiency…" />;
  if (!metrics) return <div class="muted">No metrics.</div>;

  const arb = metrics.arbitrage_efficiency_pct;
  const peak = metrics.peak_import_pct;
  const offPeak = metrics.off_peak_import_pct;
  const realisedVwap = metrics.realised_vwap_pence;
  const targetVwap = metrics.target_vwap_pence;
  const slippage = metrics.slippage_pence;
  const importKwh = metrics.today_import_kwh ?? null;
  const isSelfUseDay = importKwh != null && importKwh < SELF_USE_DAY_IMPORT_KWH_THRESHOLD;

  return (
    <div class="efficiency">
      {isSelfUseDay && (
        <div class="eff-banner">
          <span class="eff-banner-icon">☀</span>
          <div>
            <div class="eff-banner-title">Self-use day — only {importKwh!.toFixed(1)} kWh imported</div>
            <div class="eff-banner-sub">
              Battery + solar covered the load. Import-based KPIs below are
              statistically thin and shown for context only.
            </div>
          </div>
        </div>
      )}

      {arb != null && (
        <Row
          label="Arbitrage efficiency"
          subLabel="how much of the price spread the LP captured"
          value={pct(arb, 0)}
          benchmark={
            isSelfUseDay
              ? "Not applicable today — imports too small to weight"
              : benchmarkText(arb, 25, 10, "%", "captured of theoretical max")
          }
          tone={isSelfUseDay ? "neutral" : arb >= 25 ? "ok" : arb >= 10 ? "warn" : "bad"}
        />
      )}
      {peak != null && (
        <Row
          label="Imports in peak slots"
          subLabel="% of measured grid import that landed in peak-price slots"
          value={pct(peak, 0)}
          benchmark={
            isSelfUseDay
              ? "Not applicable today — imports too small to weight"
              : peak <= 15 ? "Great · ≤15% means LP avoided peak well"
              : peak <= 30 ? "OK · 15–30% is typical"
              : "High · room to shift more out of peak"
          }
          tone={isSelfUseDay ? "neutral" : peak <= 15 ? "ok" : peak <= 30 ? "warn" : "bad"}
        />
      )}
      {realisedVwap != null && targetVwap != null && slippage != null && (
        <Row
          label="Grid import VWAP vs LP target"
          subLabel="average p/kWh paid for ACTUAL grid imports (excludes self-use) vs LP plan"
          value={`${pence(realisedVwap)} / ${pence(targetVwap)}`}
          benchmark={
            isSelfUseDay
              ? "Comparison thin today — LP expected fewer imports than realised even with tiny volume"
              : slippage <= 0
                ? `Beat target · paid ${Math.abs(slippage).toFixed(1)}p less than LP ideal`
                : slippage < 2
                  ? `Tight · ${slippage.toFixed(1)}p above target`
                  : slippage < 8
                    ? `OK · ${slippage.toFixed(1)}p above target`
                    : `Wide · ${slippage.toFixed(1)}p above target — LP expected more cheap-slot imports`
          }
          tone={isSelfUseDay ? "neutral" : slippage <= 0 ? "ok" : slippage < 2 ? "ok" : slippage < 8 ? "warn" : "bad"}
        />
      )}
      {offPeak != null && (
        <Row
          label="Off-peak import share"
          subLabel="% of measured grid import in cheap / standard slots"
          value={pct(offPeak, 0)}
          benchmark={
            isSelfUseDay
              ? "Not applicable today — imports too small to weight"
              : offPeak >= 70 ? "Strong · ≥70% off-peak"
              : offPeak >= 50 ? "OK · 50–70%"
              : "Low · battery couldn't cover enough"
          }
          tone={isSelfUseDay ? "neutral" : offPeak >= 70 ? "ok" : offPeak >= 50 ? "warn" : "bad"}
        />
      )}
    </div>
  );
}

interface RowProps {
  label: string;
  subLabel: string;
  value: string;
  benchmark: string;
  tone: "ok" | "warn" | "bad" | "neutral";
}

function Row({ label, subLabel, value, benchmark, tone }: RowProps) {
  return (
    <div class={`eff-row eff-row--${tone}`}>
      <div class="eff-row-top">
        <div class="eff-row-label">{label}</div>
        <div class="eff-row-value">{value}</div>
      </div>
      <div class="eff-row-sub">{subLabel}</div>
      <div class={`eff-row-benchmark eff-row-benchmark--${tone}`}>
        <span class={`eff-row-dot eff-row-dot--${tone}`} />
        {benchmark}
      </div>
    </div>
  );
}

function benchmarkText(value: number, goodAt: number, okAt: number, unit: string, suffix: string): string {
  if (value >= goodAt) return `Good · ≥${goodAt}${unit} ${suffix}`;
  if (value >= okAt) return `OK · ${okAt}–${goodAt}${unit} ${suffix}`;
  return `Low · <${okAt}${unit} ${suffix}`;
}
