import type { AgileTodayResponse } from "../../lib/types";
import { pence, hhmm } from "../../lib/format";

interface PriceSummaryProps {
  agile: AgileTodayResponse | null;
  currentP: number | null | undefined;
  realisedVwap: number | null | undefined;
}

// Today's price stats: now, min, market avg, OUR realised VWAP (what we
// actually paid), peak. The household VWAP is the killer — it tells you
// whether the LP is actually winning the tariff.
export function PriceSummary({ agile, currentP, realisedVwap }: PriceSummaryProps) {
  const slots = agile?.import_slots ?? [];
  if (slots.length === 0) {
    return <div class="muted">No Agile rates yet.</div>;
  }

  let min = Infinity, max = -Infinity, sum = 0;
  let minSlot = "", maxSlot = "";
  for (const s of slots) {
    if (s.p < min) { min = s.p; minSlot = s.valid_from; }
    if (s.p > max) { max = s.p; maxSlot = s.valid_from; }
    sum += s.p;
  }
  const avg = sum / slots.length;
  const effectiveCurrent = currentP ?? agile?.current_import_p ?? null;

  // OUR rate vs market: lower is better (we paid less than the market average).
  const ourVsMarket = realisedVwap != null && Number.isFinite(realisedVwap) ? realisedVwap - avg : null;

  return (
    <div class="price-summary">
      <Stat label="Now" value={pence(effectiveCurrent)} sub={null} tone={effectiveCurrent != null ? toneFor(effectiveCurrent, min, max, avg) : "default"} />
      <Stat label="Today min" value={pence(min)} sub={`at ${hhmm(minSlot)}`} tone="cheap" />
      <Stat label="Market avg" value={pence(avg)} sub={null} tone="default" />
      <Stat
        label="You paid"
        value={pence(realisedVwap)}
        sub={ourVsMarket != null ? `${ourVsMarket >= 0 ? "+" : ""}${ourVsMarket.toFixed(1)}p vs market` : null}
        tone={ourVsMarket == null ? "default" : ourVsMarket < 0 ? "cheap" : "peak"}
        emphasize
      />
      <Stat label="Today peak" value={pence(max)} sub={`at ${hhmm(maxSlot)}`} tone="peak" />
    </div>
  );
}

function Stat({ label, value, sub, tone, emphasize }: { label: string; value: string; sub: string | null; tone: string; emphasize?: boolean }) {
  return (
    <div class={`price-stat price-stat--${tone}${emphasize ? " is-emphasized" : ""}`}>
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
