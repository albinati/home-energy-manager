import { useEffect, useState } from "preact/hooks";
import { useAfterPaint } from "../../lib/poll";
import { getEnergyMonthly } from "../../lib/endpoints";
import { gbp, kwh } from "../../lib/format";
import type { MonthlyEnergy } from "../../lib/types";
import "./hero.css";

// Lifetime-on-Agile totals (solar produced, exported, saved vs fixed).
// Lived in the Hero until the today-first pass — as a closing line at the
// foot of the cockpit it keeps the story without competing with today's
// bill, and its 6 /energy/monthly fetches stay off the critical path
// (self-deferred until the browser is idle after first paint).

function lastMonths(n: number): string[] {
  const now = new Date();
  const out: string[] = [];
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    out.push(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`);
  }
  return out;
}

export function LifetimeStrip() {
  const deferred = useAfterPaint();
  const [monthly, setMonthly] = useState<MonthlyEnergy[]>([]);
  useEffect(() => {
    if (!deferred) return;
    let alive = true;
    Promise.all(lastMonths(6).map((m) => getEnergyMonthly(m).catch(() => null))).then((r) => {
      if (alive) setMonthly(r.filter((x): x is MonthlyEnergy => !!x));
    });
    return () => { alive = false; };
  }, [deferred]);

  const active = monthly.filter((m) => (m.cost?.net_cost_pounds ?? 0) !== 0 || (m.energy?.export_kwh ?? 0) > 0);
  if (active.length === 0) return null;
  const lifetime = {
    months: active.length,
    solar_kwh: active.reduce((s, m) => s + (m.energy?.solar_kwh ?? 0), 0),
    export_kwh: active.reduce((s, m) => s + (m.energy?.export_kwh ?? 0), 0),
    saved_vs_fixed: active.reduce((s, m) => s + (m.cost?.delta_vs_fixed_real_pounds ?? 0), 0),
  };

  return (
    <div class="lifetime" title={`Sums across ${lifetime.months} active months on Agile`}>
      <div class="stat"><div class="stat-v">{kwh(lifetime.solar_kwh, 0)}</div><div class="stat-l">Solar produced · {lifetime.months} mo</div></div>
      <div class="stat"><div class="stat-v">{kwh(lifetime.export_kwh, 0)}</div><div class="stat-l">Exported</div></div>
      <div class="stat">
        <div class={`stat-v ${lifetime.saved_vs_fixed >= 0 ? "pos" : "neg"}`}>{gbp(Math.abs(lifetime.saved_vs_fixed))}</div>
        <div class="stat-l">{lifetime.saved_vs_fixed >= 0 ? "Saved vs fixed" : "Extra vs fixed"}</div>
      </div>
    </div>
  );
}
