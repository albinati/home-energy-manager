import type { AgileDayResponse } from "../../lib/types";
import { hhmm, pence, slotKindLabel } from "../../lib/format";

interface TariffStripProps {
  agile: AgileDayResponse | null;
  cheapP: number;
  peakP: number;
  nowUtc: string;
}

// 48-slot horizontal heatmap of today's import prices. Cells coloured by
// tariff band against `cheapP`/`peakP` thresholds. Current-slot marker shows
// where we are on the day; hover for the price + time.
export function TariffStrip({ agile, cheapP, peakP, nowUtc }: TariffStripProps) {
  if (!agile?.import || agile.import.length === 0) {
    return <div class="tariff-strip-empty muted">No Agile rates yet.</div>;
  }

  // Sort by slot time ascending.
  const slots = agile.import.slice().sort((a, b) => a.slot_time_utc.localeCompare(b.slot_time_utc));

  // Determine current-slot index by finding the slot whose start ≤ now.
  const nowMs = Date.parse(nowUtc);
  let currentIdx = -1;
  for (let i = 0; i < slots.length; i++) {
    if (Date.parse(slots[i].slot_time_utc) <= nowMs) currentIdx = i;
    else break;
  }

  return (
    <div class="tariff-strip">
      <div class="tariff-strip-cells" role="presentation">
        {slots.map((s, i) => {
          const kind = classifySlot(s.value_inc_vat, cheapP, peakP);
          const isCurrent = i === currentIdx;
          const isPast = i < currentIdx;
          return (
            <div
              key={s.slot_time_utc}
              class={`tariff-cell tariff-cell--${kind}${isCurrent ? " is-current" : ""}${isPast ? " is-past" : ""}`}
              title={`${hhmm(s.slot_time_utc)} · ${pence(s.value_inc_vat)} · ${slotKindLabel(kind)}`}
            >
              {isCurrent && <span class="tariff-cell-marker" aria-hidden="true" />}
            </div>
          );
        })}
      </div>
      <div class="tariff-strip-axis">
        <span>00</span>
        <span>06</span>
        <span>12</span>
        <span>18</span>
        <span>24</span>
      </div>
      <div class="tariff-strip-legend">
        <Swatch kind="negative" label={`Negative (< 0p)`} />
        <Swatch kind="cheap" label={`Cheap (< ${cheapP.toFixed(0)}p)`} />
        <Swatch kind="standard" label="Standard" />
        <Swatch kind="peak" label={`Peak (≥ ${peakP.toFixed(0)}p)`} />
      </div>
    </div>
  );
}

function Swatch({ kind, label }: { kind: string; label: string }) {
  return (
    <span class="tariff-swatch">
      <span class={`tariff-swatch-dot tariff-cell--${kind}`} />
      {label}
    </span>
  );
}

function classifySlot(p: number, cheapP: number, peakP: number): string {
  if (p < 0) return "negative";
  if (p < cheapP) return "cheap";
  if (p >= peakP) return "peak";
  return "standard";
}
