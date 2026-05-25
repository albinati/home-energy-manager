import type { CockpitNow, AttributionDay } from "../../lib/types";
import { kw, kwh, pence, gbp } from "../../lib/format";
import "./exports-widget.css";

interface ExportsWidgetProps {
  now: CockpitNow;
  yesterday: AttributionDay | null;
}

// Surfaces export economics: live export rate + current exporting power,
// and yesterday's total exported energy with estimated earnings (kWh ×
// average per-slot export rate). Yesterday is the freshest fully-settled
// window — today's per-slot export kWh isn't logged yet.
export function ExportsWidget({ now, yesterday }: ExportsWidgetProps) {
  const grid = now.state.grid_kw;
  const exportingNow = grid < -0.05;
  const exportRate = now.current_slot.price_export_p;
  const exportingKw = exportingNow ? -grid : 0;

  const ydayKwh = yesterday?.export_kwh ?? null;
  const ydayEarn = ydayKwh != null && exportRate != null
    ? (ydayKwh * exportRate) / 100
    : null;
  // Note: yesterday's earnings use the CURRENT export rate as an approximation
  // since we don't carry yesterday's avg export rate via this endpoint.
  // For a precise figure use /energy/report on the Plan page.

  return (
    <div class="exports-widget">
      <div class="exports-now">
        <div class="exports-now-rate" style={{ color: rateColor(exportRate) }}>
          {pence(exportRate)}
          <span class="exports-now-rate-unit">/kWh</span>
        </div>
        <div class="exports-now-rate-label">Current export rate</div>
      </div>

      <div class="exports-row exports-row-live">
        <div class="exports-cell">
          <div class="exports-cell-label">Right now</div>
          <div class="exports-cell-value" style={{ color: exportingNow ? "var(--export)" : "var(--text-mute)" }}>
            {exportingNow ? kw(exportingKw) : "—"}
          </div>
          <div class="exports-cell-sub">{exportingNow ? "exporting" : "not exporting"}</div>
        </div>
        <div class="exports-cell">
          <div class="exports-cell-label">Yesterday</div>
          <div class="exports-cell-value">{ydayKwh != null ? kwh(ydayKwh) : "—"}</div>
          <div class="exports-cell-sub">total exported</div>
        </div>
        <div class="exports-cell">
          <div class="exports-cell-label">Earnings ≈</div>
          <div class="exports-cell-value" style={{ color: "var(--ok)" }}>
            {ydayEarn != null ? gbp(ydayEarn) : "—"}
          </div>
          <div class="exports-cell-sub">yesterday</div>
        </div>
      </div>

      {ydayEarn != null && (
        <div class="exports-note">
          ≈ estimate · yesterday kWh × current export rate. Precise figure on the Plan page.
        </div>
      )}
    </div>
  );
}

function rateColor(p: number | null | undefined): string {
  if (p == null) return "var(--text-mute)";
  if (p >= 15) return "var(--ok)";
  if (p >= 5) return "var(--text)";
  return "var(--text-dim)";
}
