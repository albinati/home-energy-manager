import type { MetricsResponse, CockpitNow, AgileTodayResponse, MonthlyEnergy, PeriodInsightsResponse, TodayCumulativeResponse } from "../../lib/types";
import { gbp, kwh } from "../../lib/format";
import { useAnimatedNumber } from "../../lib/useAnimatedNumber";
import { isCurrentPeriod, periodLabel, type PeriodState } from "../../lib/period";
import { CostBreakdownChart } from "./CostBreakdownChart";
import "./hero.css";

interface HeroProps {
  metrics: MetricsResponse | null;
  metricsLoading: boolean;
  cockpit: CockpitNow | null;
  agile: AgileTodayResponse | null;
  monthly: MonthlyEnergy[];
  // The period selected in the navigator — drives the headline + savings.
  period: PeriodInsightsResponse | null;
  periodState: PeriodState;
  periodLoading: boolean;
  // Today's real-money cumulative (for the always-current "saved today" chip).
  todayCum?: TodayCumulativeResponse | null;
}

// The hero answers ONE question for the SELECTED period: how is it going on
// Agile vs the household's real alternative (the configured fixed tariff)? It
// re-scopes with the period navigator above it. The "live now" strip is the
// only always-current element. Hierarchy:
//   1. Live now strip (price + SoC) — ignores the selector
//   2. The period's real net bill (the big number)
//   3. Saved vs fixed + exported (the outcome line) — from the backend shadow
//   4. Today so far (only when viewing the current period) — explicit estimate
//   5. Lifetime totals on Agile
//   6. The cost-composition chart for the selected period
export function Hero({ metrics, cockpit, agile, monthly, period, periodState, periodLoading, todayCum }: HeroProps) {
  const isNow = isCurrentPeriod(periodState);
  const label = periodLabel(periodState);

  // --- Always-today real-money savings (independent of the period selector) ---
  // saved = £ vs the fixed-tariff shadow on the same metered kWh; pct = how much
  // of that fixed bill we erased (>100% on a paid/negative day → "100+").
  const savedToday = todayCum?.delta_vs_fixed_real_gbp ?? null;
  const netToday = todayCum?.realised_net_cost_gbp ?? null;
  const shadowToday = todayCum?.fixed_shadow_real_gbp ?? null;
  const pctOffToday = (savedToday != null && shadowToday != null && shadowToday > 0)
    ? Math.round((savedToday / shadowToday) * 100)
    : null;

  // --- The selected period, real money (NET, incl standing, measured grid) ---
  const periodNet = period?.cost?.net_cost_pounds ?? null;
  const periodExport = period?.cost?.export_earnings_pounds ?? null;

  // --- Saved vs the fixed tariff — computed BY THE BACKEND on the same metered
  // kWh + day-window as the realised cost (no Fox-vs-Octopus meter mixing). ---
  const savedVsFixed = period?.cost?.delta_vs_fixed_pounds ?? null;
  const ft = metrics?.fixed_tariff;
  const fixedLabel = ft?.label || "fixed tariff";

  // --- Today so far — estimate only (realised lands next-day). Shown only when
  // the current period is in view, so historical browsing stays clean. ---
  const todayEst = isNow && periodState.gran !== "day"
    ? (metrics?.pnl?.daily?.delta_vs_fixed_pounds ?? null)
    : null;

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
        saved_vs_fixed: activeMonths.reduce((s, m) => s + (m.cost?.delta_vs_fixed_pounds ?? 0), 0),
      }
    : null;

  // Smooth tweens for the refreshing figures.
  const periodNetAnim = useAnimatedNumber(periodNet);
  const savedAnim = useAnimatedNumber(savedVsFixed);
  const todayAnim = useAnimatedNumber(todayEst);
  const solarAnim = useAnimatedNumber(lifetime?.solar_kwh ?? null);
  const exportKwhAnim = useAnimatedNumber(lifetime?.export_kwh ?? null);
  const exportEarnAnim = useAnimatedNumber(lifetime?.export_earn ?? null);
  const totalCostAnim = useAnimatedNumber(lifetime?.total_cost ?? null);
  const savedVsFixedAnim = useAnimatedNumber(lifetime?.saved_vs_fixed ?? null);

  // Live-now strip — always current, ignores the period selector.
  const curImportP = cockpit?.current_slot?.price_import_p ?? null;
  const curExportP = agile?.current_export_p ?? cockpit?.current_slot?.price_export_p ?? null;
  const socPct = cockpit?.state?.soc_pct ?? null;

  return (
    <section class="hero" aria-label="Selected period energy outcome">
      <div class="hero-bg" aria-hidden="true" />

      <div class="hero-main">
        <div class="hero-eyebrow">
          <span class="live-pulse hero-eyebrow-dot" />
          {label} on Agile · net bill{isNow ? " so far" : ""}
        </div>
        {/* The big number is the BILL — kept neutral; the savings line below
            carries the good/bad colour. */}
        <div class="hero-headline hero-headline--enter">
          {periodNetAnim == null ? (periodLoading ? <SkelHero /> : "—") : gbp(periodNetAnim)}
        </div>
        <div class="hero-sublines">
          {savedAnim != null && (
            <div class="hero-subline">
              <strong class={savedAnim >= 0 ? "hero-strong-pos" : "hero-strong-neg"}>
                {savedAnim >= 0 ? "Saved " : "Extra "}{gbp(Math.abs(savedAnim))}
              </strong>
              &nbsp;vs {fixedLabel}
              {periodExport != null && periodExport > 0 && (
                <>&nbsp;·&nbsp;<strong class="hero-strong-pos">{gbp(periodExport)}</strong> exported</>
              )}
            </div>
          )}
          {todayAnim != null && (
            <div class="hero-subline hero-subline-dma">
              Today so far:&nbsp;
              <strong class={todayAnim >= 0 ? "hero-strong-pos" : "hero-strong-neg"}>
                {todayAnim >= 0 ? "Saved " : "Extra "}{gbp(Math.abs(todayAnim))}
              </strong>
              &nbsp;vs {fixedLabel}
              <span class="hero-est-tag" title="Estimate — today's metered cost confirms after the next-day Octopus backfill.">est</span>
            </div>
          )}
        </div>

        {(curImportP != null || socPct != null) && (
          <div class="hero-livenow" title="Live now — independent of the selected period">
            <span class="live-pulse hero-livenow-dot" />
            {curImportP != null && <span>import <strong>{curImportP.toFixed(1)}p</strong></span>}
            {curExportP != null && <span>· export <strong>{curExportP.toFixed(1)}p</strong></span>}
            {socPct != null && <span>· battery <strong>{Math.round(socPct)}%</strong></span>}
          </div>
        )}

        {savedToday != null && (savedToday > 0.005 || (netToday != null && netToday < 0)) && (
          <div class="hero-savedtoday" title="Economia real de hoje vs a tarifa fixa — sobre o consumo medido, incluindo standing charge.">
            <span class="hero-savedtoday-ico" aria-hidden="true">💚</span>
            <span>
              Hoje:&nbsp;
              {netToday != null && netToday < 0
                ? <strong class="hero-strong-pos">crédito {gbp(Math.abs(netToday))}</strong>
                : netToday != null ? <strong>{gbp(netToday)}</strong> : null}
              {savedToday > 0.005 && (
                <>&nbsp;·&nbsp;economizou&nbsp;
                  <strong class="hero-strong-pos">{gbp(savedToday)}</strong>
                  {pctOffToday != null && pctOffToday > 0 && (
                    <>&nbsp;({pctOffToday > 100 ? "100+" : pctOffToday}% abaixo do {fixedLabel})</>
                  )}
                </>
              )}
            </span>
          </div>
        )}

        {lifetime && (
          <div class="hero-lifetime" title={`Sums across ${lifetime.months} active months on Agile`}>
            <div class="hero-lifetime-label">
              Lifetime on Agile · {lifetime.months} mo
            </div>
            <div class="hero-lifetime-stats">
              <HeroStat value={kwh(solarAnim ?? 0, 0)} label="solar produced" />
              <HeroStat value={kwh(exportKwhAnim ?? 0, 0)} label="exported" />
              <HeroStat value={gbp(exportEarnAnim ?? 0)} label="export earnings" />
              <HeroStat value={gbp(totalCostAnim ?? 0)} label="total bills" />
              <HeroStat
                value={gbp(Math.abs(savedVsFixedAnim ?? 0))}
                label={(savedVsFixedAnim ?? 0) >= 0 ? "saved vs fixed" : "extra vs fixed"}
                tone={(savedVsFixedAnim ?? 0) >= 0 ? "pos" : "neg"}
                title={`Net £ vs ${fixedLabel} across ${lifetime.months} active months on Agile`}
              />
            </div>
          </div>
        )}
      </div>

      <div class="hero-status">
        <div class="hero-chart">
          <CostBreakdownChart period={period} label={label} loading={periodLoading} />
        </div>
      </div>
    </section>
  );
}

function SkelHero() {
  return <span class="skel-text" style={{ width: "8rem", height: "0.85em" }} />;
}

function HeroStat({ value, label, tone, title }: { value: string; label: string; tone?: "pos" | "neg"; title?: string }) {
  return (
    <div class="hero-stat" title={title}>
      <div class={`hero-stat-value${tone ? ` hero-stat-value--${tone}` : ""}`}>{value}</div>
      <div class="hero-stat-label">{label}</div>
    </div>
  );
}
