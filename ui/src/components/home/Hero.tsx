import type { MetricsResponse } from "../../lib/types";
import { gbpSigned } from "../../lib/format";
import "./hero.css";

interface HeroProps {
  metrics: MetricsResponse | null;
  metricsLoading: boolean;
}

// Big-number savings hero. Three comparison cells, all DAILY-NORMALISED
// (£/day) so they're directly comparable:
//   Today = literal today
//   Week  = weekly total / 7
//   Month = monthly total / day_of_month
// Eliminates the previous "today saving > whole week" visual paradox.
export function Hero({ metrics, metricsLoading }: HeroProps) {
  const daily = metrics?.pnl?.daily;
  const today = daily?.delta_vs_svt_pounds ?? null;
  const weekTotal = metrics?.pnl?.weekly?.delta_vs_svt_pounds ?? null;
  const monthTotal = metrics?.pnl?.monthly?.delta_vs_svt_pounds ?? null;
  const todayFixed = daily?.delta_vs_fixed_pounds ?? null;

  const dayOfMonth = new Date().getDate();
  const weekDaily = weekTotal != null ? weekTotal / 7 : null;
  const monthDaily = monthTotal != null ? monthTotal / Math.max(1, dayOfMonth) : null;

  const sign = (n: number | null) => (n == null ? "neutral" : n >= 0 ? "positive" : "negative");
  const heroSign = sign(today);

  const maxAbs = Math.max(Math.abs(today ?? 0), Math.abs(weekDaily ?? 0), Math.abs(monthDaily ?? 0), 1);
  const heightPct = (v: number | null) => (v == null ? 0 : Math.max(8, (Math.abs(v) / maxAbs) * 100));

  return (
    <section class="hero" aria-label="Savings overview">
      <div class="hero-bg" aria-hidden="true" />

      <div class="hero-main">
        <div class="hero-eyebrow">
          <span class="hero-eyebrow-dot" />
          Today · saved vs Standard Variable Tariff
        </div>
        <div class={`hero-headline hero-headline--${heroSign}`}>
          {today == null ? (metricsLoading ? <SkelHero /> : "—") : gbpSigned(today)}
        </div>
        <div class="hero-sublines">
          {todayFixed != null && (
            <div class="hero-subline">
              vs British Gas Fixed:&nbsp;
              <strong class={todayFixed >= 0 ? "hero-strong-pos" : "hero-strong-neg"}>
                {gbpSigned(todayFixed)}
              </strong>
            </div>
          )}
          {monthDaily != null && (
            <div class="hero-subline hero-subline-dma">
              This month's running average:&nbsp;
              <strong class={monthDaily >= 0 ? "hero-strong-pos" : "hero-strong-neg"}>
                {gbpSigned(monthDaily)}/day
              </strong>
            </div>
          )}
        </div>
      </div>

      <div class="hero-bars">
        <HeroBar
          label="Today"
          sub="actual"
          value={today}
          pct={heightPct(today)}
          sign={sign(today)}
          active
        />
        <HeroBar
          label="Week"
          sub="avg/day"
          value={weekDaily}
          totalLabel={weekTotal != null ? `${gbpSigned(weekTotal)} total` : null}
          pct={heightPct(weekDaily)}
          sign={sign(weekDaily)}
        />
        <HeroBar
          label="Month"
          sub="avg/day"
          value={monthDaily}
          totalLabel={monthTotal != null ? `${gbpSigned(monthTotal)} total` : null}
          pct={heightPct(monthDaily)}
          sign={sign(monthDaily)}
        />
      </div>
    </section>
  );
}

interface HeroBarProps {
  label: string;
  sub: string;
  value: number | null;
  totalLabel?: string | null;
  pct: number;
  sign: "positive" | "negative" | "neutral";
  active?: boolean;
}

function HeroBar({ label, sub, value, totalLabel, pct, sign, active }: HeroBarProps) {
  const color = sign === "positive" ? "var(--ok)" : sign === "negative" ? "var(--bad)" : "var(--text-mute)";
  return (
    <div class={`hero-bar${active ? " is-active" : ""}`}>
      <div class="hero-bar-track">
        <div
          class="hero-bar-fill"
          style={{
            height: `${pct}%`,
            background: `linear-gradient(180deg, ${color}33 0%, ${color} 100%)`,
          }}
        />
      </div>
      <div class="hero-bar-value" style={{ color }}>{value == null ? "—" : gbpSigned(value)}</div>
      <div class="hero-bar-sub">{sub}</div>
      <div class="hero-bar-label">{label}</div>
      {totalLabel && <div class="hero-bar-total">{totalLabel}</div>}
    </div>
  );
}

function SkelHero() {
  return <span class="skel-text" style={{ width: "8rem", height: "0.85em" }} />;
}
