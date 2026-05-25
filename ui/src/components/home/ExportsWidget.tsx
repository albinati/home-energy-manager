import type { CockpitNow, AttributionDay } from "../../lib/types";
import { kw, kwh, pence, gbp } from "../../lib/format";
import "./exports-widget.css";

interface ExportsWidgetProps {
  now: CockpitNow;
  yesterday: AttributionDay | null;
}

// Compact vertical layout that works at any widget width. Three lines:
//   1. Current export rate (big)
//   2. Live status (exporting / not exporting + kW)
//   3. Yesterday total + estimated earnings
export function ExportsWidget({ now, yesterday }: ExportsWidgetProps) {
  const grid = now.state.grid_kw;
  const exportingNow = grid < -0.05;
  const exportRate = now.current_slot.price_export_p;
  const exportingKw = exportingNow ? -grid : 0;

  const ydayKwh = yesterday?.export_kwh ?? null;
  const ydayEarn = ydayKwh != null && exportRate != null
    ? (ydayKwh * exportRate) / 100
    : null;

  return (
    <div class="exports-widget">
      <div class="exports-rate">
        <div class="exports-rate-value" style={{ color: rateColor(exportRate) }}>
          {pence(exportRate)}
        </div>
        <div class="exports-rate-label">/kWh right now</div>
      </div>

      <div class={`exports-live${exportingNow ? " is-exporting" : ""}`}>
        <span class="exports-live-dot" />
        {exportingNow ? (
          <>Exporting <strong>{kw(exportingKw)}</strong> to grid</>
        ) : (
          <>Not exporting</>
        )}
      </div>

      <div class="exports-yesterday">
        <span class="exports-yesterday-label">Yesterday</span>
        <span class="exports-yesterday-value">
          {ydayKwh != null ? kwh(ydayKwh) : "—"}
          {ydayEarn != null && (
            <span class="exports-yesterday-earn"> ≈ {gbp(ydayEarn)}</span>
          )}
        </span>
      </div>
    </div>
  );
}

function rateColor(p: number | null | undefined): string {
  if (p == null) return "var(--text-mute)";
  if (p >= 15) return "var(--ok)";
  if (p >= 5) return "var(--text)";
  return "var(--text-dim)";
}
