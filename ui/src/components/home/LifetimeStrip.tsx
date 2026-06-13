import { useEffect, useState } from "preact/hooks";
import { useAfterPaint } from "../../lib/poll";
import { getEnergyLifetime } from "../../lib/endpoints";
import { gbp, kwh } from "../../lib/format";
import type { EnergyLifetimeResponse } from "../../lib/types";
import "./hero.css";

// Lifetime-on-Agile totals (solar produced, exported, saved vs fixed) — the
// closing line at the foot of the cockpit. Now ONE cached aggregate call
// (/energy/lifetime) instead of six client-side /energy/monthly fetches, each
// of which re-ran an uncached ~1-2.7s PnL replay and serialised against the
// rest of the page load (2026-06-13 perf audit). Still self-deferred until the
// browser is idle after first paint, so it never competes with the hero.

export function LifetimeStrip() {
  const deferred = useAfterPaint();
  const [data, setData] = useState<EnergyLifetimeResponse | null>(null);
  useEffect(() => {
    if (!deferred) return;
    let alive = true;
    getEnergyLifetime(6).then((r) => { if (alive) setData(r); }).catch(() => {});
    return () => { alive = false; };
  }, [deferred]);

  if (!data || data.months === 0) return null;
  const saved = data.saved_vs_fixed_pounds;

  return (
    <div class="lifetime" title={`Sums across ${data.months} active months on Agile`}>
      <div class="stat"><div class="stat-v">{kwh(data.solar_kwh, 0)}</div><div class="stat-l">Solar produced · {data.months} mo</div></div>
      <div class="stat"><div class="stat-v">{kwh(data.export_kwh, 0)}</div><div class="stat-l">Exported</div></div>
      <div class="stat">
        <div class={`stat-v ${saved >= 0 ? "pos" : "neg"}`}>{gbp(Math.abs(saved))}</div>
        <div class="stat-l">{saved >= 0 ? "Saved vs fixed" : "Extra vs fixed"}</div>
      </div>
    </div>
  );
}
