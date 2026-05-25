import type { AgileTodayResponse } from "../../lib/types";
import { pence, hhmm } from "../../lib/format";

interface PriceSummaryProps {
  agile: AgileTodayResponse | null;
  currentP: number | null | undefined;
}

// Today's price stats: min, avg, peak, with timestamps for min/peak.
export function PriceSummary({ agile, currentP }: PriceSummaryProps) {
  const slots = agile?.import_slots ?? [];
  if (slots.length === 0) {
    return <div class="muted">No Agile rates yet.</div>;
  }

  let min = Infinity;
  let max = -Infinity;
  let sum = 0;
  let minSlot = "";
  let maxSlot = "";
  for (const s of slots) {
    if (s.p < min) { min = s.p; minSlot = s.valid_from; }
    if (s.p > max) { max = s.p; maxSlot = s.valid_from; }
    sum += s.p;
  }
  const avg = sum / slots.length;
  const effectiveCurrent = currentP ?? agile?.current_import_p ?? null;
  const currentVsAvg = effectiveCurrent != null && Number.isFinite(effectiveCurrent)
    ? effectiveCurrent - avg
    : null;

  return (
    <div class="price-summary">
      <Stat label="Now" value={pence(effectiveCurrent)} sub={currentVsAvg != null ? `${currentVsAvg >= 0 ? "+" : ""}${currentVsAvg.toFixed(1)}p vs avg` : null} tone={effectiveCurrent != null ? toneFor(effectiveCurrent, min, max, avg) : "default"} />
      <Stat label="Today min" value={pence(min)} sub={`at ${hhmm(minSlot)}`} tone="cheap" />
      <Stat label="Today avg" value={pence(avg)} sub={null} tone="default" />
      <Stat label="Today peak" value={pence(max)} sub={`at ${hhmm(maxSlot)}`} tone="peak" />
    </div>
  );
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub: string | null; tone: string }) {
  return (
    <div class={`price-stat price-stat--${tone}`}>
      <div class="price-stat-label">{label}</div>
      <div class="price-stat-value">{value}</div>
      {sub && <div class="price-stat-sub">{sub}</div>}
    </div>
  );
}

function toneFor(p: number, min: number, max: number, avg: number): string {
  if (p < 0) return "negative";
  if (p <= min + (avg - min) * 0.25) return "cheap";
  if (p >= max - (max - avg) * 0.25) return "peak";
  return "default";
}
