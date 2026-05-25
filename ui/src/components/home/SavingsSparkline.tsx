import type { MonthlyEnergy } from "../../lib/types";

interface SavingsSparklineProps {
  monthly: MonthlyEnergy[];
  width?: number;
  height?: number;
}

// Inline SVG mini bar chart: monthly savings vs SVT over the last N months.
// Green = positive (Agile beat SVT), red = negative.
export function SavingsSparkline({ monthly, width = 280, height = 60 }: SavingsSparklineProps) {
  if (monthly.length === 0) {
    return <div class="muted" style="text-align:right">No monthly history yet.</div>;
  }
  const data = monthly.slice().sort((a, b) => a.month.localeCompare(b.month));
  const values = data.map((m) => m.savings_vs_svt_gbp ?? 0);
  const max = Math.max(1, ...values.map(Math.abs));
  const barW = width / values.length;
  const zeroY = height / 2;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height} aria-label="Monthly savings sparkline">
      {values.map((v, i) => {
        const h = Math.abs(v / max) * (height / 2 - 2);
        const x = i * barW + 1;
        const y = v >= 0 ? zeroY - h : zeroY;
        const color = v >= 0 ? "var(--ok)" : "var(--bad)";
        return (
          <rect
            key={i}
            x={x}
            y={y}
            width={Math.max(2, barW - 2)}
            height={h}
            fill={color}
            rx="1"
          >
            <title>{`${data[i].month}: ${v >= 0 ? "+" : "−"}£${Math.abs(v).toFixed(2)}`}</title>
          </rect>
        );
      })}
      <line x1="0" y1={zeroY} x2={width} y2={zeroY} stroke="var(--border)" stroke-width="1" />
    </svg>
  );
}
