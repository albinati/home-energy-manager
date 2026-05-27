import type { MetricsResponse } from "../../lib/types";
import { gbpSigned } from "../../lib/format";
import "./hero.css";

interface HeroProps {
  metrics: MetricsResponse | null;
  metricsLoading: boolean;
}

// Big-number savings hero with Today / Week / Month comparison chips and
// a vs-Fixed line when /metrics carries it. Pulls from /metrics:
//   pnl.daily.delta_vs_svt_pounds + delta_vs_fixed_pounds
//   pnl.weekly.delta_vs_svt_pounds
//   pnl.monthly.delta_vs_svt_pounds
export function Hero({ metrics, metricsLoading }: HeroProps) {
  const daily = metrics?.pnl?.daily;
  const today = daily?.delta_vs_svt_pounds ?? null;
  const week = metrics?.pnl?.weekly?.delta_vs_svt_pounds ?? null;
  const month = metrics?.pnl?.monthly?.delta_vs_svt_pounds ?? null;
  const todayFixed = daily?.delta_vs_fixed_pounds ?? null;

  const sign = (n: number | null) => (n == null ? "neutral" : n >= 0 ? "positive" : "negative");
  const heroSign = sign(today);

  // For the bar scaling: normalize against the largest of (today, week, month)
  // so today's bar always reads in proportion.
  const maxAbs = Math.max(Math.abs(today ?? 0), Math.abs(week ?? 0), Math.abs(month ?? 0), 1);
  const heightPct = (v: number | null) =>
    v == null ? 0 : Math.max(8, (Math.abs(v) / maxAbs) * 100);

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
          <DmaChip month={month} />
        </div>
      </div>

      <div class="hero-bars">
        <HeroBar label="Today" value={today} pct={heightPct(today)} sign={sign(today)} active />
        <HeroBar label="Week" value={week} pct={heightPct(week)} sign={sign(week)} />
        <HeroBar label="Month" value={month} pct={heightPct(month)} sign={sign(month)} />
      </div>
    </section>
  );
}

interface HeroBarProps {
  label: string;
  value: number | null;
  pct: number;
  sign: "positive" | "negative" | "neutral";
  active?: boolean;
}

function HeroBar({ label, value, pct, sign, active }: HeroBarProps) {
  const color = sign === "positive" ? "var(--ok)" : sign === "negative" ? "var(--bad)" : "var(--text-mute)";
  return (
    <div class={`hero-bar${active ? " is-active" : ""}`}>
      <div class="hero-bar-track">
        <div
          class="hero-bar-fill"
          style={{
            height: `${pct}%`,
            background: `linear-gradient(180deg, ${color}33 0%, ${color} 100%)`,
            transform: sign === "negative" ? "scaleY(-1)" : undefined,
            transformOrigin: "bottom",
          }}
        />
      </div>
      <div class="hero-bar-value" style={{ color }}>{value == null ? "—" : gbpSigned(value)}</div>
      <div class="hero-bar-label">{label}</div>
    </div>
  );
}

function SkelHero() {
  return <span class="skel-text" style={{ width: "8rem", height: "0.85em" }} />;
}

function DmaChip({ month }: { month: number | null }) {
  if (month == null) return null;
  const dayOfMonth = new Date().getDate();
  const dma = month / Math.max(1, dayOfMonth);
  return (
    <div class="hero-subline hero-subline-dma">
      30-day average:&nbsp;
      <strong class={dma >= 0 ? "hero-strong-pos" : "hero-strong-neg"}>
        {gbpSigned(dma)}/day
      </strong>
    </div>
  );
}
