import type { MetricsResponse, CockpitNow, AgileTodayResponse, MonthlyEnergy, PeriodInsightsResponse } from "../../lib/types";
import { gbp, gbpSigned, kwh } from "../../lib/format";
import { useAnimatedNumber } from "../../lib/useAnimatedNumber";
import { CostBreakdownChart } from "./CostBreakdownChart";
import "./hero.css";

interface HeroProps {
  metrics: MetricsResponse | null;
  metricsLoading: boolean;
  cockpit: CockpitNow | null;
  agile: AgileTodayResponse | null;
  monthly: MonthlyEnergy[];
  todayPeriod: PeriodInsightsResponse | null;
  monthPeriod: PeriodInsightsResponse | null;
  periodsLoading: boolean;
}

// SEG export floor used when comparing against a flat fixed tariff (which
// wouldn't have Agile's outgoing rate). Matches TariffComparisonWidget.
const SEG_EXPORT_FALLBACK_P = 4.0;

// The hero answers ONE question clearly: how is this month going on Agile vs the
// household's real alternative (British Gas Fixed)?  We lead with the MONTH —
// it's the figure backed by complete, metered data — because *today* only fills
// in after the next-day Octopus backfill, so an intraday "today" headline would
// be empty or estimated. Hierarchy:
//   1. This month's real net bill (the big number)
//   2. Saved vs BG Fixed + exported (the outcome line)
//   3. Today so far — small, explicitly an estimate
//   4. Lifetime totals on Agile
//   5. The cost-composition chart (Today / 7d / Month)
export function Hero({ metrics, cockpit, agile, monthly, todayPeriod, monthPeriod, periodsLoading }: HeroProps) {
  const monthName = new Date().toLocaleDateString([], { month: "long" });

  // --- This month, real money ---
  const monthNet = monthPeriod?.cost?.net_cost_pounds ?? null;          // £ out the door
  const monthExport = monthPeriod?.cost?.export_earnings_pounds ?? null;
  const cd = monthPeriod?.chart_data ?? [];
  const monthDays = cd.length;
  const monthImportKwh = cd.reduce((s, d) => s + (d.import_kwh ?? 0), 0);
  const monthExportKwh = cd.reduce((s, d) => s + (d.export_kwh ?? 0), 0);

  // --- Saved vs British Gas Fixed (computed client-side from the same real
  // usage block + the configured FIXED_TARIFF_* rates — the engine's
  // delta_vs_fixed uses a different MANUAL_TARIFF rate, so we don't use it). ---
  const ft = metrics?.fixed_tariff;
  let savedVsFixed: number | null = null;
  if (ft?.label && ft.rate_pence && monthNet != null && monthDays > 0) {
    const bgImport = monthImportKwh * ft.rate_pence / 100;
    const bgStanding = monthDays * (ft.standing_pence_per_day ?? 0) / 100;
    const bgExport = monthExportKwh * SEG_EXPORT_FALLBACK_P / 100;
    const bgNet = bgImport + bgStanding - bgExport;
    savedVsFixed = bgNet - monthNet;
  }
  const fixedLabel = ft?.label || "fixed tariff";

  // --- Today so far — estimate only (realised lands next-day). The engine's
  // daily fixed delta is the available "today savings" figure. ---
  const todayEst = metrics?.pnl?.daily?.delta_vs_fixed_pounds ?? null;

  // --- Lifetime totals on Agile (folded in from the old Lifetime widget) ---
  const activeMonths = monthly.filter(
    (m) => (m.cost?.net_cost_pounds ?? 0) !== 0 || (m.energy?.export_kwh ?? 0) > 0,
  );
  const lifetime = activeMonths.length > 0
    ? {
        months: activeMonths.length,
        solar_kwh: activeMonths.reduce((s, m) => s + (m.energy?.solar_kwh ?? 0), 0),
        export_kwh: activeMonths.reduce((s, m) => s + (m.energy?.export_kwh ?? 0), 0),
        export_earn: activeMonths.reduce((s, m) => s + (m.cost?.export_earnings_pounds ?? 0), 0),
        total_cost: activeMonths.reduce((s, m) => s + (m.cost?.net_cost_pounds ?? 0), 0),
      }
    : null;

  // Smooth tweens for the refreshing figures.
  const monthNetAnim = useAnimatedNumber(monthNet);
  const savedAnim = useAnimatedNumber(savedVsFixed);
  const todayAnim = useAnimatedNumber(todayEst);
  const solarAnim = useAnimatedNumber(lifetime?.solar_kwh ?? null);
  const exportKwhAnim = useAnimatedNumber(lifetime?.export_kwh ?? null);
  const exportEarnAnim = useAnimatedNumber(lifetime?.export_earn ?? null);
  const totalCostAnim = useAnimatedNumber(lifetime?.total_cost ?? null);
  const curExportP = agile?.current_export_p ?? cockpit?.current_slot?.price_export_p ?? null;

  return (
    <section class="hero" aria-label="This month's energy outcome">
      <div class="hero-bg" aria-hidden="true" />

      <div class="hero-main">
        <div class="hero-eyebrow">
          <span class="live-pulse hero-eyebrow-dot" />
          {monthName} on Agile · net bill so far
        </div>
        {/* The big number is the BILL — kept neutral; the savings line below
            carries the good/bad colour. */}
        <div class="hero-headline hero-headline--enter">
          {monthNetAnim == null ? (periodsLoading ? <SkelHero /> : "—") : gbp(monthNetAnim)}
        </div>
        <div class="hero-sublines">
          {savedAnim != null && (
            <div class="hero-subline">
              <strong class={savedAnim >= 0 ? "hero-strong-pos" : "hero-strong-neg"}>
                {savedAnim >= 0 ? "Saved " : "Cost "}{gbpSigned(savedAnim)}
              </strong>
              &nbsp;vs {fixedLabel}
              {monthExport != null && monthExport > 0 && (
                <>&nbsp;·&nbsp;<strong class="hero-strong-pos">{gbp(monthExport)}</strong> exported</>
              )}
            </div>
          )}
          {todayAnim != null && (
            <div class="hero-subline hero-subline-dma">
              Today so far:&nbsp;
              <strong class={todayAnim >= 0 ? "hero-strong-pos" : "hero-strong-neg"}>
                {gbpSigned(todayAnim)}
              </strong>
              &nbsp;vs {fixedLabel}
              <span class="hero-est-tag" title="Estimate — today's metered cost confirms after the next-day Octopus backfill.">est</span>
            </div>
          )}
        </div>

        {lifetime && (
          <div class="hero-lifetime" title={`Sums across ${lifetime.months} active months on Agile`}>
            <div class="hero-lifetime-label">
              Lifetime on Agile · {lifetime.months} mo
              {curExportP != null ? ` · export now ${curExportP.toFixed(1)}p/kWh` : ""}
            </div>
            <div class="hero-lifetime-stats">
              <HeroStat value={kwh(solarAnim ?? 0, 0)} label="solar produced" />
              <HeroStat value={kwh(exportKwhAnim ?? 0, 0)} label="exported" />
              <HeroStat value={gbp(exportEarnAnim ?? 0)} label="export earnings" />
              <HeroStat value={gbp(totalCostAnim ?? 0)} label="total bills" />
            </div>
          </div>
        )}
      </div>

      <div class="hero-status">
        <div class="hero-chart">
          <CostBreakdownChart today={todayPeriod} month={monthPeriod} loading={periodsLoading} />
        </div>
      </div>
    </section>
  );
}

function SkelHero() {
  return <span class="skel-text" style={{ width: "8rem", height: "0.85em" }} />;
}

function HeroStat({ value, label }: { value: string; label: string }) {
  return (
    <div class="hero-stat">
      <div class="hero-stat-value">{value}</div>
      <div class="hero-stat-label">{label}</div>
    </div>
  );
}
