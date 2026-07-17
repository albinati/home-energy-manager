import { useEffect, useRef } from "preact/hooks";
import { useFetch, useInflight } from "../lib/poll";
import { getFairCompare } from "../lib/endpoints";
import { usePeriod, periodLabel, isCurrentPeriod } from "../lib/period";
import { PeriodNavigator } from "../components/shell/PeriodNavigator";
import { Pill } from "../components/common/Pill";
import { gbp, gbpSigned, kwh } from "../lib/format";
import { makeChart, baseOption, chartTheme, barGradient, withAlpha, type EChartsType } from "../lib/charts";
import type { FairTariffRow } from "../lib/types";
import { LoadPatternCard } from "../components/insights/LoadPatternCard";
import { LoadForecastAccuracyCard } from "../components/insights/LoadForecastAccuracyCard";
import { SystemHealthCard } from "../components/insights/SystemHealthCard";
import { IndoorHistoryCard } from "../components/insights/IndoorHistoryCard";
import { SolarClearnessCard } from "../components/insights/SolarClearnessCard";
import "./insights.css";

const p2 = (p: number) => gbp(p / 100);

// Fair tariff comparison: the household's MEASURED per-slot usage replayed
// against every tariff's own rate card (per-tariff standing + export; negative
// imports credit the bill). Scoped by the shared day/week/month/year navigator.
export default function Insights() {
  const period = usePeriod();
  const inflight = useInflight();
  const cmp = useFetch(
    () => getFairCompare(period.gran, period.anchor),
    [period.gran, period.anchor],
    { cacheKey: `fair:${period.gran}:${period.anchor}`, immutable: !isCurrentPeriod(period), track: true },
  );
  const data = cmp.data;
  const rows = data?.tariffs ?? [];
  const current = rows.find((r) => r.is_current) ?? null;
  const winner = rows.find((r) => r.product_code === data?.winner_product_code) ?? null;
  const winnerIsCurrent = winner?.is_current ?? false;
  // The household's previous fixed tariff (synthetic "FIXED" row), for the
  // "saving £X vs <old tariff>" line — mirrors the cockpit header framing.
  const fixedRow = rows.find((r) => r.product_code === "FIXED") ?? null;

  return (
    <div class="page-padded insights">
      <header class="insights-head">
        <h1>Insights</h1>
        <p class="muted">
          Every tariff priced on your <strong>actual metered usage</strong> — each with its own
          standing charge and export rate; negative-price imports credit the bill.
        </p>
      </header>

      <PeriodNavigator variant="page" />

      {/* One shared cue for the whole period-scoped page: the cards below fetch
          independently with very different latencies (the fair-compare replay +
          heatmap rebuild take seconds; the rest are instant), so without this
          the page updates piecemeal and looks half-stale on navigation. */}
      <div class={`insights-updating${inflight > 0 ? " is-on" : ""}`} role="status" aria-live="polite">
        <span class="insights-updating-bar" />
        <span class="insights-updating-label">Updating {periodLabel(period)}…</span>
      </div>

      {/* The first compare for a period is a heavy server-side replay (can
          take seconds before the TTL cache warms). A ghost table reads as
          progress; a lone spinner reads as "stuck". */}
      {cmp.loading && !data && (
        <div class="skel-table" aria-label="Comparing tariffs" role="status">
          <div class="skel-table-head">
            <span class="skel-text" style={{ width: "9rem" }} />
            <span class="skel-text" style={{ width: "5rem" }} />
          </div>
          {Array.from({ length: 8 }, (_, i) => (
            <div class="skel-table-row" key={i}>
              <span class="skel-text" style={{ width: `${11 - (i % 3)}rem` }} />
              <span class="skel-text" style={{ width: "3.5rem" }} />
              <span class="skel-text" style={{ width: "3rem" }} />
              <span class="skel-text" style={{ width: "4rem" }} />
            </div>
          ))}
          <p class="muted small">Comparing tariffs on your metered usage…</p>
        </div>
      )}
      {cmp.error && <p class="insights-error">Couldn't load the comparison: {cmp.error.message}</p>}

      {data && rows.length === 0 && (
        <p class="muted insights-empty">
          No metered data for {periodLabel(period)}{data.clamped ? ` (since ${data.period_start})` : ""}.
        </p>
      )}

      {data && rows.length > 0 && (
        <div class={`insights-compare${cmp.loading ? " is-updating" : ""}`}>
          {/* Winner banner — suppressed when there's too little metered usage to
              compare meaningfully (otherwise "you're cheapest" reads trivially). */}
          {data.basis.import_kwh < 1 ? (
            <div class="insights-winner insights-winner--nodata">
              <span>
                Not enough metered usage in {periodLabel(period)} yet to compare tariffs —
                the rows below show standing charges only. Pick an earlier period with data.
              </span>
            </div>
          ) : (
            <div class={`insights-winner${winnerIsCurrent ? " is-current-best" : ""}`}>
              {winnerIsCurrent ? (
                <span>
                  You're on the cheapest tariff for {periodLabel(period)} — <strong>{winner?.display_name}</strong>.
                  {fixedRow && current && fixedRow.net_pence > current.net_pence && (
                    <> Saving <strong>{gbp((fixedRow.net_pence - current.net_pence) / 100)}</strong> vs {fixedRow.display_name}.</>
                  )}
                </span>
              ) : (
                <span>
                  Cheapest for {periodLabel(period)}: <strong>{winner?.display_name}</strong> —
                  saves <strong>{gbp(data.savings_vs_current_pounds)}</strong> vs your tariff
                  {winner?.approximate ? " (approx)" : ""}.
                </span>
              )}
            </div>
          )}

          {/* kWh basis + qualifiers */}
          <div class="insights-basis">
            <span><strong>{kwh(data.basis.import_kwh)}</strong> imported · <strong>{kwh(data.basis.export_kwh)}</strong> exported</span>
            <span class="insights-basis-sep">·</span>
            <span>{data.days_with_data}/{data.n_days} days metered</span>
            {data.clamped && <span class="insights-basis-warn"> · since {data.period_start} (pre-Agile days excluded)</span>}
            {data.catalogue_unavailable && <span class="insights-basis-warn"> · live catalogue offline (showing SVT/fixed only)</span>}
          </div>

          {/* Reconciliation — the realised headline the user asked for */}
          {current && current.import_kwh > 0 && (
            <div class="insights-reconcile">
              Imported <strong>{kwh(current.import_kwh)}</strong> at an average of
              {" "}<strong>{(current.import_cost_pence / current.import_kwh).toFixed(1)}p/kWh</strong>
              {" "}· net after export <strong>{p2(current.net_pence)}</strong>
              {current.negative_credit_pence < -0.5 && (
                <span class="insights-reconcile-credit"> (incl. {p2(-current.negative_credit_pence)} negative-price credit)</span>
              )}
            </div>
          )}

          {/* Export valuation — on Outgoing Agile show the actual Agile earnings;
              only on the old flat SEG show the SEG-vs-Agile switch opportunity. */}
          {data.export && data.export.export_kwh > 0.05 && (
            <div class="insights-export-panel">
              <span class="insights-export-title">Export</span>
              {data.export.mode === "outgoing_agile" ? (
                <span>
                  <strong>{kwh(data.export.export_kwh)}</strong> exported · earned
                  {" "}<strong>{p2(data.export.agile_revenue_pence)}</strong> on Outgoing Agile
                  (~{data.export.agile_avg_p.toFixed(1)}p avg)
                </span>
              ) : (
                <>
                  <span>
                    <strong>{kwh(data.export.export_kwh)}</strong> exported · paid
                    {" "}<strong>{p2(data.export.seg_revenue_pence)}</strong> at SEG {data.export.seg_rate_p.toFixed(2)}p (current)
                  </span>
                  <span class="insights-export-alt">
                    would earn <strong>{p2(data.export.agile_revenue_pence)}</strong> on Outgoing Agile
                    (~{data.export.agile_avg_p.toFixed(1)}p avg)
                    {data.export.uplift_if_switch_pence > 1 && (
                      <span class="insights-export-uplift"> → +{p2(data.export.uplift_if_switch_pence)} if you switch</span>
                    )}
                  </span>
                </>
              )}
            </div>
          )}

          {/* Standing-charges-only state (import_kwh < 1) shows the table only;
              a comparison chart there would imply a real comparison. */}
          {data.basis.import_kwh >= 1 && <ComparisonChart rows={rows} />}

          {/* Table */}
          <div class="insights-table-wrap">
            <table class="insights-table">
              <thead>
                <tr>
                  <th>Tariff</th>
                  <th class="num">Import</th>
                  <th class="num">Standing</th>
                  <th class="num">Export</th>
                  <th class="num">Net</th>
                  <th class="num">If you switch</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <Row key={r.product_code} r={r} curNet={current?.net_pence ?? r.net_pence} />
                ))}
              </tbody>
            </table>
          </div>
          <p class="insights-note muted">
            Net = import + standing − export credit, all on the same measured usage.
            <span class="insights-legend-approx">*</span> = a half-hourly tariff other than
            yours, priced by proxy (its per-slot rates aren't published). Export valued at each
            tariff's own rate (0 where it offers none; SVT/fixed assume a standard SEG).
          </p>
        </div>
      )}

      <SolarClearnessCard />
      <IndoorHistoryCard />
      <LoadPatternCard period={period} />
      <LoadForecastAccuracyCard period={period} />
      <SystemHealthCard period={period} />
    </div>
  );
}

function Row({ r, curNet }: { r: FairTariffRow; curNet: number }) {
  // How YOUR bill would change if you switched TO this tariff.
  //   + (costs more) → staying on yours is right → muted, not alarming.
  //   − (cheaper)    → a genuine opportunity → highlighted green.
  // (Earlier this showed every dearer alternative as a red negative — a table of
  //  red "−£38" next to the "you're cheapest" banner read as if we were losing.)
  const billChange = (r.net_pence - curNet) / 100;
  const cheaper = billChange < -0.005;
  return (
    <tr class={r.is_current ? "is-current" : ""}>
      <td class="insights-name">
        <span class="insights-dot" />
        {r.display_name}
        {r.is_current && <Pill tone="accent">NOW</Pill>}
        {r.approximate && <span class="insights-approx" title="Priced by proxy — non-current half-hourly tariff">*</span>}
        {r.negative_credit_pence < -0.5 && (
          <span class="insights-sub">incl. {p2(-r.negative_credit_pence)} back from negative prices</span>
        )}
      </td>
      <td class="num">{p2(r.import_cost_pence)}</td>
      <td class="num">{p2(r.standing_pence)}</td>
      <td class="num insights-export">{r.export_credit_pence > 0.5 ? `−${p2(r.export_credit_pence)}` : "—"}</td>
      <td class="num insights-net">{p2(r.net_pence)}</td>
      <td class="num">
        {r.is_current ? "—" : (
          <span
            class={cheaper ? "insights-cheaper" : "insights-costlier"}
            title={cheaper ? "Cheaper than your tariff on this usage" : "You'd pay this much more on this tariff"}
          >
            {gbpSigned(billChange)}
          </span>
        )}
      </td>
    </tr>
  );
}

// Stacked bar per tariff: import + standing above zero, export credit below.
function ComparisonChart({ rows }: { rows: FairTariffRow[] }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const ch = makeChart(ref.current);
    chartRef.current = ch;
    const onResize = () => ch.resize();
    window.addEventListener("resize", onResize);
    return () => { window.removeEventListener("resize", onResize); ch.dispose(); chartRef.current = null; };
  }, []);

  useEffect(() => {
    if (!chartRef.current || !rows.length) return;
    const t = chartTheme();
    const base = baseOption();
    const labels = rows.map((r) => r.display_name);
    const imp = rows.map((r) => Math.round(r.import_cost_pence) / 100);
    const stand = rows.map((r) => Math.round(r.standing_pence) / 100);
    const exp = rows.map((r) => -Math.round(r.export_credit_pence) / 100);
    const net = rows.map((r) => Math.round(r.net_pence) / 100);

    chartRef.current.setOption({
      ...base,
      grid: { left: 8, right: 16, top: 28, bottom: 8, containLabel: true },
      legend: { ...(base.legend as object), show: true, top: 0, left: "center",
        data: ["Import", "Standing", "Export credit"] },
      tooltip: {
        ...(base.tooltip as object),
        formatter: (ps: Array<{ dataIndex: number }>) => {
          const i = ps[0]?.dataIndex ?? 0; const r = rows[i];
          return `<strong>${r.display_name}</strong>${r.is_current ? " (yours)" : ""}<br/>` +
            `Import ${gbp(r.import_cost_pence / 100)}<br/>Standing ${gbp(r.standing_pence / 100)}<br/>` +
            `Export −${gbp(r.export_credit_pence / 100)}<br/><strong>Net ${gbp(r.net_pence / 100)}</strong>`;
        },
      },
      xAxis: { ...(base.xAxis as object), type: "category", data: labels,
        axisLabel: { color: t.textMute, fontSize: 9, interval: 0, rotate: 32, width: 90, overflow: "truncate" } },
      yAxis: [{ ...(base.yAxis as object), axisLabel: { color: t.textMute, fontSize: 10, formatter: "£{value}" } }],
      series: [
        { name: "Import", type: "bar", stack: "c", data: imp,
          itemStyle: { color: barGradient(t.importColor, 0.9, 0.5) }, barMaxWidth: 38 },
        { name: "Standing", type: "bar", stack: "c", data: stand,
          itemStyle: { color: withAlpha(t.textMute, 0.6) } },
        { name: "Export credit", type: "bar", stack: "c", data: exp,
          itemStyle: { color: barGradient(t.ok, 0.85, 0.45) } },
        // Net marker on top (a thin line series at the net value, label only).
        { name: "Net", type: "line", data: net, symbol: "circle", symbolSize: 7,
          showSymbol: true, lineStyle: { opacity: 0 }, z: 5,
          itemStyle: { color: t.text },
          label: { show: true, position: "top", formatter: (o: { value: number }) => `£${o.value.toFixed(2)}`,
                   color: t.text, fontSize: 10, fontWeight: 600 } },
      ],
    }, { notMerge: true });
  }, [rows]);

  return <div ref={ref} class="insights-chart" role="img" aria-label="Tariff cost comparison — your usage priced on each tariff" />;
}
