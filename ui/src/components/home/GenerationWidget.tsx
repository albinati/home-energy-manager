import { chartTheme, withAlpha } from "../../lib/charts";
import { useFetch } from "../../lib/poll";
import { getPvToday, getGridToday } from "../../lib/endpoints";
import { MetricTimeline, localHM, nowIndexOf, periodPointLabel, type TimelineLine } from "./MetricTimeline";
import { Spinner } from "../common/Spinner";
import { gbp } from "../../lib/format";
import type { PeriodInsightsResponse, PvTodayResponse, GridTodayResponse, AgileTodayResponse, ExportOpportunityResponse } from "../../lib/types";
import { isCurrentPeriod, type PeriodState } from "../../lib/period";
import "./timeline-widget.css";

interface Props {
  period: PeriodState;
  periodData: PeriodInsightsResponse | null;
  periodLoading: boolean;
  // Octopus rates — export_slots drives the price line, import_slots the zones.
  agile: AgileTodayResponse | null;
  // Running tally of export £ lost on flat SEG vs Outgoing Agile.
  opportunity?: ExportOpportunityResponse | null;
  cheapP?: number | null;
  peakP?: number | null;
}

// The "money left on the table" line — only meaningful while on flat SEG.
function OppyLine({ o }: { o?: ExportOpportunityResponse | null }) {
  if (!o || o.export_mode !== "seg_flat" || o.opportunity_gbp <= 0.01) return null;
  return (
    <div class="tlw-oppy" title={`Sobre ${o.n_days} dias de export: SEG pagou ${gbp(o.seg_gbp)} vs Outgoing Agile ${gbp(o.agile_gbp)}. A diferença é o que deixamos de ganhar por não estar na Agile (média SEG ${o.avg_seg_p}p vs Agile ${o.avg_agile_p}p/kWh).`}>
      💸 perdido vs Agile: <strong>{gbp(o.opportunity_gbp)}</strong> em {o.n_days}d
      {o.annualized_gbp > 0 && <> · ~{gbp(o.annualized_gbp)}/ano</>}
      {o.today.opportunity_gbp > 0.005 && <> · hoje {gbp(o.today.opportunity_gbp)}</>}
    </div>
  );
}

// GENERATION = what the house produced and fed back. Day → solar committed-plan
// vs realised + grid export, with the Octopus EXPORT price slots on the right
// axis and the cheap/peak/negative tariff zones shaded (from the import price —
// the canonical tariff context). Week/month/year → daily solar + export bars.
export function GenerationWidget({ period, periodData, periodLoading, agile, opportunity }: Props) {
  const isDay = period.gran === "day";
  const dayArg = isCurrentPeriod(period) ? undefined : period.anchor;
  const day = useFetch(
    () => (isDay
      ? Promise.all([getPvToday(dayArg), getGridToday(dayArg)])
          .then(([p, g]) => ({ p, g }) as { p: PvTodayResponse; g: GridTodayResponse })
      : Promise.resolve(null)),
    [isDay, dayArg],
  );
  const t = chartTheme();

  if (isDay) {
    const pv = day.data?.p;
    const grid = day.data?.g;
    const slots = pv?.slots ?? [];
    if (day.loading && !slots.length) return <Spinner label="Loading generation…" />;
    if (!slots.length) return <p class="muted">No generation data for this day yet.</p>;
    const labels = slots.map((s) => localHM(s.slot_utc));
    const nowIdx = nowIndexOf(slots, pv?.now_utc);
    const solarPlan = slots.map((s, i) =>
      nowIdx >= 0 && i > nowIdx ? round2(s.pv_forecast_kwh) : round2(s.pv_planned_kwh ?? s.pv_forecast_kwh));
    const solarActual = slots.map((s) => (s.pv_actual_kwh == null ? null : round2(s.pv_actual_kwh)));
    // Grid export realised, aligned to the pv axis by slot_utc.
    const expBy = new Map<string, number>();
    for (const gs of grid?.slots ?? []) {
      if (gs.slot_utc && gs.export_actual_kwh != null) expBy.set(gs.slot_utc, gs.export_actual_kwh);
    }
    const exportActual = slots.map((s) => {
      const v = expBy.get(s.slot_utc);
      return v == null ? null : round2(v);
    });
    // Export prices on the right axis. SEG flat (what we earn today) = the green
    // line; the Outgoing Agile curve (what we WOULD earn) overlaid in amber so
    // the gap between them is the opportunity. On Outgoing Agile the green line
    // IS the curve and there's no separate comparison.
    const segRate = agile?.export_seg_rate_p ?? null;
    const onSeg = segRate != null;
    const expPriceBy = new Map<string, number>();
    for (const es of agile?.export_slots ?? []) expPriceBy.set(normZ(es.valid_from), es.p);
    const agileExportPrice = slots.map((s) => expPriceBy.get(normZ(s.slot_utc)) ?? null);
    const exportPrice = onSeg ? slots.map(() => segRate) : agileExportPrice;
    const lines: TimelineLine[] = [
      { name: "Solar plan", color: withAlpha(t.pv, 0.5), data: solarPlan, dashed: true },
      { name: "Solar", color: t.pv, data: solarActual, area: true, width: 3 },
      { name: "Export", color: t.exportColor, data: exportActual, width: 1.75 },
      ...(onSeg ? [{ name: "Outgoing Agile", color: t.warn, data: agileExportPrice, dashed: true, isPrice: true } as TimelineLine] : []),
    ];
    const nowMs = pv?.now_utc ? new Date(pv.now_utc).getTime() : Date.now();
    const genTotal = slots.reduce((sum, s) => {
      const elapsed = new Date(s.slot_utc).getTime() + 30 * 60_000 <= nowMs;
      return sum + ((elapsed ? (s.pv_actual_kwh ?? s.pv_forecast_kwh) : s.pv_forecast_kwh) ?? 0);
    }, 0);
    const exportedTotal = grid?.totals?.export_actual_kwh ?? 0;
    return (
      <div class="tlw">
        <div class="tlw-summary">
          <span class="tlw-summary-value">{genTotal.toFixed(1)}<span class="tlw-summary-unit"> kWh solar</span></span>
          <span class="tlw-summary-value tlw-pos">{exportedTotal.toFixed(1)}<span class="tlw-summary-unit"> kWh export</span></span>
          <span class="tlw-summary-label">esperado hoje{segRate != null ? ` · export pago a ${segRate.toFixed(1)}p/kWh (SEG flat)` : ""}</span>
        </div>
        <OppyLine o={opportunity} />
        {/* Export price line (green, right axis) mirrors Consumption's import
            line; the cheap/peak/negative ZONES stay on Consumption only (that's
            the import tariff) so the two timelines don't repeat the same bands. */}
        <MetricTimeline labels={labels} lines={lines} prices={exportPrice}
                        priceLabel={onSeg ? "Export (SEG)" : "Export price"} priceColor={t.exportColor}
                        nowIdx={nowIdx} height={270} />
        <div class="tlw-legend">
          <span><i style={`border-color:${t.pv}`} /> solar actual</span>
          <span><i class="dashed" style={`border-color:${withAlpha(t.pv, 0.6)}`} /> solar plan</span>
          <span><i style={`border-color:${t.exportColor}`} /> export kWh</span>
          <span><i class="dashed" style={`border-color:${t.exportColor}`} /> export {onSeg ? `${segRate.toFixed(1)}p SEG` : "price"}</span>
          {onSeg && <span><i class="dashed" style={`border-color:${t.warn}`} /> Outgoing Agile (alvo)</span>}
          <span>◉ now</span>
        </div>
      </div>
    );
  }

  // Period mode: daily solar + export bars.
  const pts = periodData?.chart_data ?? [];
  if (periodLoading && !pts.length) return <Spinner label="Loading generation…" />;
  if (!pts.length) return <p class="muted">No generation data for this period.</p>;
  const labels = pts.map((p) => periodPointLabel(p.date, period.gran));
  const lines: TimelineLine[] = [
    { name: "Solar", color: t.pv, data: pts.map((p) => round2(p.solar_kwh)), area: true },
    { name: "Export", color: t.exportColor, data: pts.map((p) => round2(p.export_kwh)) },
  ];
  const solarTot = pts.reduce((s, p) => s + (p.solar_kwh ?? 0), 0);
  const expTot = pts.reduce((s, p) => s + (p.export_kwh ?? 0), 0);
  return (
    <div class="tlw">
      <div class="tlw-summary">
        <span class="tlw-summary-value">{solarTot.toFixed(0)}<span class="tlw-summary-unit"> kWh solar</span></span>
        <span class="tlw-summary-value tlw-pos">{expTot.toFixed(0)}<span class="tlw-summary-unit"> kWh export</span></span>
        <span class="tlw-summary-label">geração · {periodData?.period_label}</span>
      </div>
      <OppyLine o={opportunity} />
      <MetricTimeline labels={labels} lines={lines} barMode height={240} />
    </div>
  );
}

function normZ(iso: string): string {
  try { return new Date(iso).toISOString().replace(".000Z", "Z"); } catch { return iso; }
}
function round2(n: number): number {
  return Math.round(n * 100) / 100;
}
