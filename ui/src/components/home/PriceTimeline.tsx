import type { AgileTodayResponse } from "../../lib/types";
import "./price-timeline.css";

interface PriceTimelineProps {
  agile: AgileTodayResponse | null;
  cheapP?: number;
  peakP?: number;
}

const TIER_COLOR: Record<string, string> = {
  negative: "var(--neg, #38bdf8)",
  cheap: "var(--cheap, #36d399)",
  standard: "var(--text-mute, #8a8f98)",
  peak: "var(--peak, #f87171)",
};

function tierOf(p: number, cheapP?: number, peakP?: number): string {
  if (p < 0) return "negative";
  if (cheapP != null && p <= cheapP) return "cheap";
  if (peakP != null && p >= peakP) return "peak";
  return "standard";
}

// SVG mini price-timeline (no ECharts — the hero is above the fold). Today's
// per-slot import price as a tier-coloured bar sparkline with a now-marker.
// Replaces the import/standing/export cost bars, which "didn't say much".
export function PriceTimeline({ agile, cheapP, peakP }: PriceTimelineProps) {
  const slots = agile?.import_slots ?? [];
  if (slots.length === 0) {
    return (
      <div class="price-tl price-tl--empty">
        <span class="muted">Preços de hoje indisponíveis</span>
      </div>
    );
  }

  const W = 320, H = 116, padT = 6, padB = 4, padX = 3;
  const prices = slots.map((s) => s.p);
  const min = Math.min(0, ...prices);
  const max = Math.max(...prices, 1);
  const span = max - min || 1;
  const n = slots.length;
  const bw = (W - 2 * padX) / n;
  const y = (p: number) => padT + (1 - (p - min) / span) * (H - padT - padB);
  const zeroY = y(0);

  const nowMs = agile?.now_utc ? Date.parse(agile.now_utc) : Date.now();
  let nowIdx = -1;
  for (let i = 0; i < n; i++) {
    const a = Date.parse(slots[i].valid_from);
    const b = Date.parse(slots[i].valid_to);
    if (nowMs >= a && nowMs < b) { nowIdx = i; break; }
  }

  return (
    <div class="price-tl">
      <div class="price-tl-head">
        <span>Preço de importação · hoje</span>
        {agile?.current_import_p != null && (
          <strong>{agile.current_import_p.toFixed(1)}p</strong>
        )}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} class="price-tl-svg" preserveAspectRatio="none"
           role="img" aria-label="Preço de importação por slot, hoje">
        <line x1={padX} x2={W - padX} y1={zeroY} y2={zeroY}
              stroke="var(--border)" stroke-width="1" stroke-dasharray="2 3" opacity="0.5" />
        {slots.map((s, i) => {
          const tier = s.kind || tierOf(s.p, cheapP, peakP);
          const col = TIER_COLOR[tier] || TIER_COLOR.standard;
          const yp = y(s.p);
          const top = Math.min(yp, zeroY);
          const h = Math.max(0.6, Math.abs(yp - zeroY));
          const past = nowIdx >= 0 && i < nowIdx;
          return (
            <rect key={i} x={padX + i * bw} y={top} width={Math.max(0.5, bw - 0.4)}
                  height={h} fill={col} opacity={past ? 0.4 : 0.92} rx={0.4} />
          );
        })}
        {nowIdx >= 0 && (
          <line x1={padX + (nowIdx + 0.5) * bw} x2={padX + (nowIdx + 0.5) * bw}
                y1={padT} y2={H - padB} stroke="var(--accent)" stroke-width="1.5" opacity="0.9" />
        )}
      </svg>
      <div class="price-tl-legend">
        <span><i style="background:var(--neg,#38bdf8)" />pago</span>
        <span><i style="background:var(--cheap,#36d399)" />barato</span>
        <span><i style="background:var(--peak,#f87171)" />pico</span>
      </div>
    </div>
  );
}
