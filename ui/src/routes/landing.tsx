import { useEffect, useState } from "preact/hooks";
import { Link } from "wouter-preact";
import { useFetch } from "../lib/poll";
import { getEnergyReport, getAttributionDay, getEnergyMonthly } from "../lib/endpoints";
import { Spinner } from "../components/common/Spinner";
import { Card } from "../components/common/Card";
import { AttributionDonut } from "../components/landing/AttributionDonut";
import { SavingsTrend } from "../components/landing/SavingsTrend";
import { gbp, gbpSigned, kwh } from "../lib/format";
import type { MonthlyEnergy } from "../lib/types";
import "../components/landing/landing.css";

// Last N month-codes ending on the current month. e.g. ["2026-01", "2026-02", ...]
function lastMonths(n: number): string[] {
  const now = new Date();
  const out: string[] = [];
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    out.push(`${yyyy}-${mm}`);
  }
  return out;
}

function useMonthlyHistory(n: number) {
  const [data, setData] = useState<MonthlyEnergy[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    const months = lastMonths(n);
    Promise.all(
      months.map((m) =>
        getEnergyMonthly(m).catch(() => null)
      ),
    ).then((results) => {
      if (!alive) return;
      setData(results.filter((r): r is MonthlyEnergy => !!r));
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, [n]);

  return { data, loading };
}

export default function Landing() {
  const report = useFetch(getEnergyReport, []);
  const attribution = useFetch(getAttributionDay, []);
  const monthly = useMonthlyHistory(6);

  const savingsThisMonth = monthly.data[monthly.data.length - 1]?.savings_vs_svt_gbp ?? null;
  const savingsTotal = monthly.data.reduce((acc, m) => acc + (m.savings_vs_svt_gbp ?? 0), 0);
  const fixedDelta =
    report.data?.pnl?.delta_vs_fixed_real_gbp ??
    report.data?.pnl?.delta_vs_fixed_tariff_real_gbp ??
    report.data?.pnl?.delta_vs_fixed_gbp ??
    null;

  return (
    <div class="landing">
      {/* Hero */}
      <section class="landing-hero">
        <div class="landing-hero-inner">
          <div>
            <span class="landing-eyebrow">
              <span class="landing-hero-eyebrow-dot" aria-hidden="true"></span>
              Live in production
            </span>
            <h1 class="landing-hero-title">
              A home that <br/>
              <span class="landing-hero-title-savings">{renderHeroSavings(savingsTotal)}</span>{" "}
              by thinking for itself.
            </h1>
            <p class="landing-hero-sub">
              Solar, a battery, a heat pump, and Octopus Agile — orchestrated by a small
              Python service that forecasts the day ahead, solves an LP for the cheapest
              dispatch, and writes the plan straight into the inverter. No taps. No taps
              missed. No surprises on the bill.
            </p>
            <div class="landing-hero-cta">
              <Link href="/cockpit" class="btn btn--primary">Open the live cockpit →</Link>
              <Link href="/forecast" class="btn">See today's forecast</Link>
            </div>
          </div>
          <aside class="landing-hero-card" aria-label="Headline saving">
            <div class="landing-hero-card-label">Last {monthly.data.length || "—"} months</div>
            <div class="landing-hero-card-value">{gbp(savingsTotal)}</div>
            <div class="landing-hero-card-detail">
              versus the standard variable tariff — the cumulative effect of cheap-rate charging,
              peak-time discharging, and solar self-use.
            </div>
            {fixedDelta != null && (
              <div class="landing-hero-card-detail" style={{ marginTop: "0.6rem" }}>
                Today vs fixed: <strong style={{ color: fixedDelta >= 0 ? "var(--ok)" : "var(--bad)" }}>{gbpSigned(fixedDelta)}</strong>
              </div>
            )}
          </aside>
        </div>
      </section>

      {/* KPI strip */}
      <section class="landing-kpi-strip">
        <div class="landing-kpi">
          <span class="landing-kpi-label">This month vs SVT</span>
          <span class={`landing-kpi-value ${savingsThisMonth != null && savingsThisMonth >= 0 ? "landing-kpi-accent" : "landing-kpi-bad"}`}>
            {savingsThisMonth != null ? gbpSigned(savingsThisMonth) : "—"}
          </span>
          <span class="landing-kpi-detail">
            Energy savings against the variable tariff for the current calendar month.
          </span>
        </div>
        <div class="landing-kpi">
          <span class="landing-kpi-label">Yesterday's solar</span>
          <span class="landing-kpi-value">
            {attribution.data?.solar_kwh != null ? kwh(attribution.data.solar_kwh) : "—"}
          </span>
          <span class="landing-kpi-detail">
            {attribution.data?.shares
              ? `${attribution.data.shares.self_use_pct.toFixed(0)}% self-used, ${attribution.data.shares.battery_pct.toFixed(0)}% to battery, ${attribution.data.shares.export_pct.toFixed(0)}% exported.`
              : "Solar production attribution by destination."}
          </span>
        </div>
        <div class="landing-kpi">
          <span class="landing-kpi-label">Exported</span>
          <span class="landing-kpi-value">
            {attribution.data?.export_kwh != null ? kwh(attribution.data.export_kwh) : "—"}
          </span>
          <span class="landing-kpi-detail">
            kWh sold back at the Outgoing Agile rate yesterday.
          </span>
        </div>
      </section>

      {/* Charts */}
      <section class="landing-section">
        <h2 class="landing-section-title">The receipts</h2>
        <p class="landing-section-sub">
          The system files a daily PnL against shadow tariffs. Months that beat the
          variable tariff are green; months that lost are red. No averages, no spin.
        </p>
        <div class="landing-twocol">
          <Card title="Monthly savings vs SVT" subtitle="Bars are signed: positive = Agile beat SVT, negative = it lost.">
            {monthly.loading ? (
              <Spinner label="Crunching monthly PnL…" />
            ) : monthly.data.length === 0 ? (
              <p class="muted">No monthly data yet — system needs ≥ 1 day of Agile history.</p>
            ) : (
              <SavingsTrend monthly={monthly.data} />
            )}
          </Card>
          <Card title="Where yesterday's solar went" subtitle="Self-use vs battery-charged vs exported, from the attribution report.">
            {attribution.loading ? (
              <Spinner label="Loading attribution…" />
            ) : attribution.error || !attribution.data ? (
              <p class="muted">No attribution data yet.</p>
            ) : (
              <AttributionDonut data={attribution.data} />
            )}
          </Card>
        </div>
      </section>

      {/* How it works */}
      <section class="landing-section">
        <h2 class="landing-section-title">How it works</h2>
        <p class="landing-section-sub">
          Three jobs in a loop, every 30 minutes. No black-box ML, just an LP solver
          and a robustness filter over forecast scenarios.
        </p>
        <div class="landing-threecol">
          <Link href="/forecast" class="feature-card">
            <div class="feature-card-step">1</div>
            <div class="feature-card-title">Forecast</div>
            <div class="feature-card-body">
              Pull Quartz solar + Open-Meteo weather + Octopus Agile rates. Apply a
              14-day calibration factor to handle local microclimate. See the inputs.
            </div>
            <span class="feature-card-link">View forecast →</span>
          </Link>
          <Link href="/settings" class="feature-card">
            <div class="feature-card-step">2</div>
            <div class="feature-card-title">Optimise</div>
            <div class="feature-card-body">
              Linear-program the next 48 half-hours. Plan cheap-rate charging,
              peak-export windows, and DHW thermal storage. Re-solve at every tier
              boundary plus on forecast drift.
            </div>
            <span class="feature-card-link">Tune settings →</span>
          </Link>
          <Link href="/cockpit" class="feature-card">
            <div class="feature-card-step">3</div>
            <div class="feature-card-title">Dispatch</div>
            <div class="feature-card-body">
              Write the plan to the Fox inverter and the Daikin heat pump. Watch every
              slot fire in the cockpit, with the LP's reasoning attached.
            </div>
            <span class="feature-card-link">Open cockpit →</span>
          </Link>
        </div>
      </section>
    </div>
  );
}

function renderHeroSavings(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "saves money quietly";
  if (Math.abs(n) < 1) return "saves money quietly";
  return `saved ${gbp(n, 0)}`;
}
