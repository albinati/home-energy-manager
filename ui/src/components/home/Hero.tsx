import type { MetricsResponse, CockpitNow, AgileTodayResponse, MonthlyEnergy, PeriodInsightsResponse, TodayCumulativeResponse } from "../../lib/types";
import { gbp, kwh } from "../../lib/format";
import { useAnimatedNumber } from "../../lib/useAnimatedNumber";
import { isCurrentPeriod, periodLabel, type PeriodState } from "../../lib/period";
import { PriceTimeline } from "./PriceTimeline";
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

  // --- TODAY's real money (the hero money block, independent of the period
  // selector). Uses the CONFIGURED fixed-tariff (British Gas) comparison — NOT
  // the generic ~23p fixed shadow that mislabels + inflates the saving. ---
  const gastoToday = todayCum?.realised_net_cost_gbp ?? null;          // net bill (<0 = credit)
  const savedVsBG = todayCum?.delta_vs_fixed_tariff_real_gbp ?? null;  // £ cheaper than British Gas
  const fixedLabel = todayCum?.fixed_tariff_label || metrics?.fixed_tariff?.label || "British Gas";
  const earningsToday = todayCum?.earnings_today_gbp ?? null;          // negative-import credit + export
  const negCreditToday = todayCum?.negative_import_credit_gbp ?? null;
  const exportToday = todayCum?.export_revenue_gbp ?? null;
  const standingToday = todayCum?.standing_charge_gbp ?? null;         // fixed daily standing in the net
  const showEarnings = (earningsToday ?? 0) > 0.005;                   // hide on a plain spend day

  // --- The selected period, real money (NET, incl standing, measured grid) ---
  const periodNet = period?.cost?.net_cost_pounds ?? null;

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
        // Use the authoritative slot-level British-Gas comparison
        // (delta_vs_fixed_real_pounds), NOT the coarse Fox-energy delta which
        // flips sign on Agile months and counts pre-Agile months. null months
        // (pre-Agile) contribute 0, so this is the on-Agile saving.
        saved_vs_fixed: activeMonths.reduce((s, m) => s + (m.cost?.delta_vs_fixed_real_pounds ?? 0), 0),
      }
    : null;

  // Smooth tweens for the refreshing figures.
  const periodNetAnim = useAnimatedNumber(periodNet);
  const savedVsBGAnim = useAnimatedNumber(savedVsBG);
  const gastoTodayAnim = useAnimatedNumber(gastoToday);
  const earningsTodayAnim = useAnimatedNumber(earningsToday);
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
        {/* TODAY money block — one British-Gas comparison (deduped), the day's
            bill, and the concrete money earned (negative-import credit + export). */}
        <div class="hero-sublines">
          {savedVsBGAnim != null && (
            <div class="hero-subline">
              <strong class={savedVsBGAnim >= 0 ? "hero-strong-pos" : "hero-strong-neg"}>
                {savedVsBGAnim >= 0 ? "Economizou " : "Gastou +"}{gbp(Math.abs(savedVsBGAnim))}
              </strong>
              &nbsp;hoje vs {fixedLabel}
            </div>
          )}
          <div class="hero-subline hero-subline-dma">
            Conta hoje:&nbsp;
            {gastoTodayAnim == null ? "—"
              : gastoTodayAnim < 0
                ? <strong class="hero-strong-pos">crédito {gbp(Math.abs(gastoTodayAnim))}</strong>
                : <strong>{gbp(gastoTodayAnim)}</strong>}
            {showEarnings && earningsTodayAnim != null && (
              <span class="hero-earnings" title="Dinheiro que entrou hoje: crédito da importação a preço negativo + receita de export. A standing charge fixa do dia é descontada para chegar na conta.">
                &nbsp;·&nbsp;⚡ foi pago&nbsp;<strong class="hero-strong-pos">{gbp(earningsTodayAnim)}</strong>
                {(negCreditToday ?? 0) > 0.005 && (exportToday ?? 0) > 0.005
                  ? <> ({gbp(negCreditToday!)} negativo + {gbp(exportToday!)} export)</>
                  : (negCreditToday ?? 0) > 0.005
                    ? <> (import negativo)</>
                    : <> (export)</>}
                {(standingToday ?? 0) > 0.005 && (
                  <span class="hero-standing"> &minus; {gbp(standingToday!)} standing</span>
                )}
              </span>
            )}
          </div>
        </div>

        {(curImportP != null || socPct != null) && (
          <div class="hero-livenow" title="Live now — independent of the selected period">
            <span class="live-pulse hero-livenow-dot" />
            {curImportP != null && <span>import <strong>{curImportP.toFixed(1)}p</strong></span>}
            {curExportP != null && <span>· export <strong>{curExportP.toFixed(1)}p</strong></span>}
            {socPct != null && <span>· battery <strong>{Math.round(socPct)}%</strong></span>}
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
          <PriceTimeline
            agile={agile}
            cheapP={metrics?.cheap_threshold_pence}
            peakP={metrics?.peak_threshold_pence}
          />
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
