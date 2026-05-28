import type { MetricsResponse, CockpitNow, AgileTodayResponse, MonthlyEnergy } from "../../lib/types";
import { gbp, gbpSigned, kw, kwh, pct } from "../../lib/format";
import { useAnimatedNumber } from "../../lib/useAnimatedNumber";
import "./hero.css";

interface HeroProps {
  metrics: MetricsResponse | null;
  metricsLoading: boolean;
  cockpit: CockpitNow | null;
  agile: AgileTodayResponse | null;
  monthly: MonthlyEnergy[];
}

// The hero is the at-a-glance "where are we right now" surface. It bundles:
//   1. Savings vs SVT (the biggest number on the screen)
//   2. Comparison vs the previous BG Fixed contract
//   3. Today's running DMA (£/day)
//   4. Lifetime totals on Agile (folded in from the old Lifetime widget)
//   5. Live "motion" — action verb + tariff band + import/export p/kWh
// Numeric values are tweened via useAnimatedNumber so refreshes feel alive
// rather than snapping.
export function Hero({ metrics, metricsLoading, cockpit, agile, monthly }: HeroProps) {
  const daily = metrics?.pnl?.daily;
  const today = daily?.delta_vs_svt_pounds ?? null;
  const todayFixed = daily?.delta_vs_fixed_pounds ?? null;
  const monthTotal = metrics?.pnl?.monthly?.delta_vs_svt_pounds ?? null;

  const dayOfMonth = new Date().getDate();
  const monthDaily = monthTotal != null ? monthTotal / Math.max(1, dayOfMonth) : null;

  const sign = today == null ? "neutral" : today >= 0 ? "positive" : "negative";

  const state = cockpit?.state;
  const motion = state ? inferMotion(state) : null;
  const currentImport = agile?.current_import_p ?? null;
  const currentExport = agile?.current_export_p ?? null;
  const importBand = classifyBand(currentImport, metrics?.cheap_threshold_pence, metrics?.peak_threshold_pence);

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

  // Smooth tweens for every refreshing figure.
  const todayAnim = useAnimatedNumber(today);
  const todayFixedAnim = useAnimatedNumber(todayFixed);
  const monthDailyAnim = useAnimatedNumber(monthDaily);
  const solarAnim = useAnimatedNumber(lifetime?.solar_kwh ?? null);
  const exportKwhAnim = useAnimatedNumber(lifetime?.export_kwh ?? null);
  const exportEarnAnim = useAnimatedNumber(lifetime?.export_earn ?? null);
  const totalCostAnim = useAnimatedNumber(lifetime?.total_cost ?? null);
  const importAnim = useAnimatedNumber(currentImport);
  const exportRateAnim = useAnimatedNumber(currentExport);
  const socAnim = useAnimatedNumber(state?.soc_pct ?? null);

  return (
    <section class="hero" aria-label="Live status + savings overview">
      <div class="hero-bg" aria-hidden="true" />

      <div class="hero-main">
        <div class="hero-eyebrow">
          <span class="hero-eyebrow-dot" />
          Today · saved vs Standard Variable Tariff
        </div>
        <div class={`hero-headline hero-headline--${sign}`}>
          {todayAnim == null ? (metricsLoading ? <SkelHero /> : "—") : gbpSigned(todayAnim)}
        </div>
        <div class="hero-sublines">
          {todayFixedAnim != null && (
            <div class="hero-subline">
              vs British Gas Fixed:&nbsp;
              <strong class={todayFixedAnim >= 0 ? "hero-strong-pos" : "hero-strong-neg"}>
                {gbpSigned(todayFixedAnim)}
              </strong>
            </div>
          )}
          {monthDailyAnim != null && (
            <div class="hero-subline hero-subline-dma">
              This month's running average:&nbsp;
              <strong class={monthDailyAnim >= 0 ? "hero-strong-pos" : "hero-strong-neg"}>
                {gbpSigned(monthDailyAnim)}/day
              </strong>
            </div>
          )}
        </div>

        {lifetime && (
          <div class="hero-lifetime" title={`Sums across ${lifetime.months} active months on Agile`}>
            <div class="hero-lifetime-label">
              Lifetime on Agile · {lifetime.months} mo
            </div>
            <div class="hero-lifetime-stats">
              <HeroStat value={kwh(solarAnim ?? 0, 0)} label="solar produced" tone="pv" />
              <HeroStat value={kwh(exportKwhAnim ?? 0, 0)} label="exported" tone="export" />
              <HeroStat value={gbp(exportEarnAnim ?? 0)} label="export earnings" tone="ok" />
              <HeroStat value={gbp(totalCostAnim ?? 0)} label="total bills" tone="cost" />
            </div>
          </div>
        )}
      </div>

      <div class="hero-status">
        {motion && (
          <div class="hero-motion">
            <span class="hero-motion-icon" style={{ background: motion.color, boxShadow: `0 0 16px ${motion.color}66` }}>
              {motion.icon}
            </span>
            <div class="hero-motion-text">
              <div class="hero-motion-title" style={{ color: motion.color }}>{motion.title}</div>
              <div class="hero-motion-sub">{motion.sub}</div>
            </div>
          </div>
        )}

        <div class="hero-tariff">
          <div class={`hero-tariff-band hero-tariff-band--${importBand}`}>
            <span class="hero-tariff-band-dot" />
            <span>{labelForBand(importBand)}</span>
          </div>
          <div class="hero-tariff-rates">
            <div class="hero-tariff-rate">
              <span class="hero-tariff-rate-label">Import</span>
              <span class="hero-tariff-rate-value">{importAnim != null ? `${importAnim.toFixed(2)}p` : "—"}</span>
            </div>
            <div class="hero-tariff-rate">
              <span class="hero-tariff-rate-label">Export</span>
              <span class="hero-tariff-rate-value">{exportRateAnim != null ? `${exportRateAnim.toFixed(2)}p` : "—"}</span>
            </div>
            {socAnim != null && (
              <div class="hero-tariff-rate">
                <span class="hero-tariff-rate-label">Battery</span>
                <span class="hero-tariff-rate-value">{pct(socAnim, 0)}</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

interface Motion { title: string; sub: string; icon: string; color: string; }

function inferMotion(s: { grid_kw: number; battery_kw: number; solar_kw: number; load_kw: number }): Motion {
  const grid = s.grid_kw, batt = s.battery_kw, solar = s.solar_kw, load = s.load_kw, E = 0.1;
  const importing = grid > E, exporting = grid < -E;
  const charging = batt > E, discharging = batt < -E;
  const producing = solar > E;
  if (discharging && exporting) return { title: "Exporting from battery", sub: `${kw(-batt + Math.max(0, solar))} → grid`, icon: "⚡", color: "var(--peak-export)" };
  if (exporting && !discharging) return { title: "Exporting solar", sub: `${kw(-grid)} → grid · ${kw(load)} house`, icon: "☀", color: "var(--export)" };
  if (charging && importing) return { title: "Charging from grid", sub: `${kw(grid)} import · ${kw(batt)} into battery`, icon: "⚡", color: "var(--cheap)" };
  if (charging && producing) return { title: "Charging from solar", sub: `${kw(solar)} solar · ${kw(batt)} into battery`, icon: "⚡", color: "var(--pv)" };
  if (discharging) return { title: "Battery → house", sub: `${kw(-batt)} battery · ${kw(load)} load`, icon: "🔋", color: "var(--warn)" };
  if (importing) return { title: "Importing from grid", sub: `${kw(grid)} import · ${kw(load)} house`, icon: "⬇", color: "var(--import)" };
  if (producing) return { title: "Self-using solar", sub: `${kw(solar)} solar · ${kw(load)} house`, icon: "☀", color: "var(--pv)" };
  return { title: "Holding", sub: `${kw(load)} house · waiting`, icon: "•", color: "var(--text-mute)" };
}

type Band = "negative" | "cheap" | "standard" | "peak" | "unknown";

function classifyBand(p: number | null | undefined, cheapAt?: number, peakAt?: number): Band {
  if (p == null) return "unknown";
  if (p < 0) return "negative";
  if (cheapAt != null && p <= cheapAt) return "cheap";
  if (peakAt != null && p >= peakAt) return "peak";
  return "standard";
}

function labelForBand(b: Band): string {
  if (b === "negative") return "Paid to import";
  if (b === "cheap") return "Cheap";
  if (b === "peak") return "Peak";
  if (b === "standard") return "Standard";
  return "—";
}

function SkelHero() {
  return <span class="skel-text" style={{ width: "8rem", height: "0.85em" }} />;
}

function HeroStat({ value, label, tone }: { value: string; label: string; tone: "pv" | "export" | "ok" | "cost" }) {
  return (
    <div class={`hero-stat hero-stat--${tone}`}>
      <div class="hero-stat-value">{value}</div>
      <div class="hero-stat-label">{label}</div>
    </div>
  );
}
