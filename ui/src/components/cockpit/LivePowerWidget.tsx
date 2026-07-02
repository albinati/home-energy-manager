import { useEffect, useState } from "preact/hooks";
import { PowerFlow } from "./PowerFlow";
import { reducedMotion } from "../../lib/motion";
import { kwh, gbp } from "../../lib/format";
import type { CockpitState, CockpitNow, AgileTodayResponse, MetricsResponse, TodayCumulativeResponse } from "../../lib/types";
import "./cockpit.css";
import "./live-power.css";

interface LivePowerWidgetProps {
  state: CockpitState;
  cockpit: CockpitNow;
  agile: AgileTodayResponse | null;
  metrics: MetricsResponse | null;
  todayCumulative?: TodayCumulativeResponse | null;
}

// Redesign form: the animated power-flow IS the surface (no separate focal
// number — the node field carries the watts), with a quiet rates row beneath
// it (Import / Export p + today's kWh·£) and the compact battery SoC block on
// the right. The committed plan renders as the PlanMini foot (composed by the
// landing route inside the same card).
export function LivePowerWidget({ state, cockpit, agile, metrics, todayCumulative }: LivePowerWidgetProps) {
  const socPct = state.soc_pct ?? 0;
  const charging = state.battery_kw > 0.05;
  const discharging = state.battery_kw < -0.05;
  const mode = charging ? "charging" : discharging ? "discharging" : "idle";

  const importP = agile?.current_import_p ?? null;
  const exportP = agile?.current_export_p ?? cockpit.current_slot?.price_export_p ?? null;
  const importBand = classifyBand(importP, metrics?.cheap_threshold_pence, metrics?.peak_threshold_pence);

  return (
    <div class="livepower">
      <div class="livepower-flow">
        <PowerFlow state={state} />
      </div>

      <div class="rates">
        <div class="rate" title="Live Agile import p/kWh + how much you've imported so far today (to now)">
          <div class="lp-rate-k">Import</div>
          <div class={`lp-rate-v livepower-rate--band-${importBand}`}>{importP != null ? `${importP.toFixed(1)}p` : "—"}</div>
          {todayCumulative && (
            <div class="lp-rate-sub">
              {kwh(todayCumulative.import_kwh)} ·{" "}
              {todayCumulative.import_cost_gbp < -0.005
                ? <span class="livepower-credit" title="Paid to import on negative-price slots">+{gbp(Math.abs(todayCumulative.import_cost_gbp))} credit</span>
                : <span>{gbp(todayCumulative.import_cost_gbp)}</span>} today
            </div>
          )}
        </div>
        <div class="rate" title="Live export p/kWh + what you've exported so far today">
          <div class="lp-rate-k">Export</div>
          {exportP != null
            ? <div class="lp-rate-v livepower-rate--export">{exportP.toFixed(1)}p</div>
            : <div class="lp-rate-v lp-rate-v--none">no export</div>}
          {todayCumulative && (
            <div class="lp-rate-sub">
              {kwh(todayCumulative.export_kwh)} ·{" "}
              <span class="livepower-rate--export">{gbp(todayCumulative.export_revenue_gbp)}</span> today
            </div>
          )}
        </div>
        <span class="grow" />
        <BatterySOC soc={socPct} kwhVal={state.soc_kwh} mode={mode} kw={Math.abs(state.battery_kw)} />
      </div>
    </div>
  );
}

type Band = "negative" | "cheap" | "standard" | "peak" | "unknown";

function classifyBand(p: number | null | undefined, cheapAt?: number, peakAt?: number): Band {
  if (p == null) return "unknown";
  if (p < 0) return "negative";
  if (cheapAt != null && p <= cheapAt) return "cheap";
  if (peakAt != null && p >= peakAt) return "peak";
  return "standard";
}

// Compact battery state-of-charge block (redesign .batt-soc): a small glyph
// whose fill + text colour follow the MODE (charging green / discharging
// amber / idle mute), with %, kWh and a status line. The fill springs from 0
// to the live SoC on mount (skipped under reduced motion).
function BatterySOC({ soc, kwhVal, mode, kw }: { soc: number; kwhVal: number | null | undefined; mode: "charging" | "discharging" | "idle"; kw: number }) {
  // Read per render, not at module load — the in-app motion toggle
  // (lib/motion.ts) can change at runtime and a module-level const would
  // pin this widget to the boot-time preference until a full reload.
  const RM = reducedMotion();
  const fill = Math.max(0, Math.min(1, soc / 100));
  const color = mode === "charging" ? "var(--ok)" : mode === "discharging" ? "var(--peak)" : "var(--text-mute)";
  const innerH = 48;

  const [f, setF] = useState(RM ? fill : 0);
  useEffect(() => {
    if (RM) { setF(fill); return; }
    const id = requestAnimationFrame(() => setF(fill));
    return () => cancelAnimationFrame(id);
  }, [fill]);

  return (
    <div class="batt-soc">
      <svg viewBox="0 0 40 66" class="batt-glyph" aria-hidden="true">
        <rect x="13" y="2" width="14" height="4" rx="1.5" fill="var(--border-strong)" />
        <rect x="6" y="9" width="28" height="53" rx="6" fill="none" stroke="var(--border-strong)" stroke-width="2.5" />
        <rect x="9.5" y={12 + (1 - f) * innerH} width="21" height={f * innerH} rx="3" fill={color}
              style={{ transition: RM ? "none" : "y 700ms var(--ease-lock), height 700ms var(--ease-lock), fill 200ms ease" }} />
      </svg>
      <div class="batt-tx">
        <div class="batt-pct" style={{ color }}>{Math.round(soc)}%</div>
        <div class="batt-kwh">{kwh(kwhVal)}</div>
        <div class="batt-status" style={{ color }}>
          <span class="batt-dot" style={{ background: color }} />
          {mode === "charging" ? "Charging" : mode === "discharging" ? "Discharging" : "Idle"}
          {kw > 0.05 ? ` · ${kw.toFixed(1)} kW` : ""}
        </div>
      </div>
    </div>
  );
}
