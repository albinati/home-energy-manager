import type { AgileTodayResponse, CockpitNow } from "../../lib/types";
import { hhmm, pence, slotKindLabel } from "../../lib/format";

interface TariffWidgetProps {
  agile: AgileTodayResponse | null;
  now: CockpitNow;
}

// Current price hero + 48-cell strip with current-time marker. Designed
// to live inside a widget chrome so it stays compact.
export function TariffWidget({ agile, now }: TariffWidgetProps) {
  const importSlots = agile?.import_slots ?? [];
  const cheapP = now.thresholds?.cheap_p ?? 12;
  const peakP = now.thresholds?.peak_p ?? 28;

  if (importSlots.length === 0) {
    return <div class="muted">No Agile rates yet.</div>;
  }

  const sorted = importSlots.slice().sort((a, b) => a.valid_from.localeCompare(b.valid_from));
  const nowMs = Date.parse(now.now_utc);

  let currentIdx = -1;
  for (let i = 0; i < sorted.length; i++) {
    if (Date.parse(sorted[i].valid_from) <= nowMs) currentIdx = i;
    else break;
  }
  const currentSlot = currentIdx >= 0 ? sorted[currentIdx] : null;
  const currentP = currentSlot?.p ?? now.current_slot.price_import_p;
  const currentKind = currentSlot?.kind || classifySlot(currentP, cheapP, peakP);
  const currentColor = colorFor(currentKind);

  // Next slot whose band differs from current — countdown anchor.
  let nextChange: { iso: string; kind: string; p: number } | null = null;
  if (currentIdx >= 0) {
    for (let i = currentIdx + 1; i < sorted.length; i++) {
      const k = sorted[i].kind || classifySlot(sorted[i].p, cheapP, peakP);
      if (k !== currentKind) {
        nextChange = { iso: sorted[i].valid_from, kind: k, p: sorted[i].p };
        break;
      }
    }
  }
  const minutesUntil = nextChange ? Math.max(0, Math.round((Date.parse(nextChange.iso) - nowMs) / 60000)) : null;

  // Stats
  let min = Infinity, max = -Infinity, sum = 0;
  for (const s of sorted) {
    if (s.p < min) min = s.p;
    if (s.p > max) max = s.p;
    sum += s.p;
  }
  const avg = sum / sorted.length;

  return (
    <div class="tariff-widget">
      <div class="tariff-widget-now">
        <div class="tariff-widget-now-main">
          <div class="tariff-widget-now-price" style={{ color: currentColor }}>
            {pence(currentP)}
          </div>
          <div class="tariff-widget-now-band" style={{ color: currentColor }}>
            <span class="tariff-widget-now-dot" style={{ background: currentColor }} />
            {slotKindLabel(currentKind)}
          </div>
        </div>
        {nextChange && minutesUntil != null && (
          <div class="tariff-widget-next" style={{ borderLeftColor: colorFor(nextChange.kind) }}>
            <div class="tariff-widget-next-label">Next change in</div>
            <div class="tariff-widget-next-value">{formatCountdown(minutesUntil)}</div>
            <div class="tariff-widget-next-sub">
              → <span style={{ color: colorFor(nextChange.kind) }}>{slotKindLabel(nextChange.kind)}</span> {pence(nextChange.p)}
            </div>
          </div>
        )}
      </div>

      <div class="tariff-widget-strip">
        <div class="tariff-strip-cells" role="presentation">
          {sorted.map((s, i) => {
            const k = s.kind || classifySlot(s.p, cheapP, peakP);
            const isCurrent = i === currentIdx;
            const isPast = i < currentIdx;
            return (
              <div
                key={s.valid_from}
                class={`tariff-cell tariff-cell--${k}${isCurrent ? " is-current" : ""}${isPast ? " is-past" : ""}`}
                title={`${hhmm(s.valid_from)} · ${pence(s.p)} · ${slotKindLabel(k)}`}
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
      </div>

      <div class="tariff-widget-summary">
        <SummaryStat label="Today min" value={pence(min)} color="var(--cheap)" />
        <SummaryStat label="Today avg" value={pence(avg)} color="var(--text-dim)" />
        <SummaryStat label="Today peak" value={pence(max)} color="var(--peak)" />
      </div>
    </div>
  );
}

function SummaryStat({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div class="tariff-summary-stat">
      <div class="tariff-summary-label" style={{ color }}>{label}</div>
      <div class="tariff-summary-value">{value}</div>
    </div>
  );
}

function classifySlot(p: number, cheapP: number, peakP: number): string {
  if (p < 0) return "negative";
  if (p < cheapP) return "cheap";
  if (p >= peakP) return "peak";
  return "standard";
}
function colorFor(kind: string): string {
  switch (kind) {
    case "negative": return "var(--neg-price)";
    case "cheap": case "solar_charge": case "solar_preheat": return "var(--cheap)";
    case "peak": return "var(--peak)";
    case "peak_export": return "var(--peak-export)";
    default: return "var(--standard)";
  }
}
function formatCountdown(min: number): string {
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  const m = min % 60;
  return m === 0 ? `${h} h` : `${h}h ${m}m`;
}
