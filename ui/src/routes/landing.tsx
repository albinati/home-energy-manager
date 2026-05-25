import { useEffect, useState } from "preact/hooks";
import { usePoll, useFetch } from "../lib/poll";
import {
  getCockpitNow,
  getEnergyReport,
  getEnergyMonthly,
  getAgileToday,
  getWeather,
} from "../lib/endpoints";
import { Card } from "../components/common/Card";
import { Spinner } from "../components/common/Spinner";
import { TariffStrip } from "../components/cockpit/TariffStrip";
import { ComingUp } from "../components/home/ComingUp";
import { SavingsSparkline } from "../components/home/SavingsSparkline";
import { gbp, gbpSigned, kw, kwh, pct, tempC } from "../lib/format";
import type { MonthlyEnergy } from "../lib/types";
import "../components/home/home.css";

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

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
    Promise.all(months.map((m) => getEnergyMonthly(m).catch(() => null))).then((results) => {
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
  const now = usePoll(getCockpitNow, 20_000);
  const report = useFetch(() => getEnergyReport(todayIso()), []);
  const agile = useFetch(getAgileToday, []);
  const weather = useFetch(getWeather, []);
  const monthly = useMonthlyHistory(6);

  if (now.loading && !now.data) {
    return (
      <div class="home">
        <Spinner label="Loading dashboard…" />
      </div>
    );
  }
  if (!now.data) {
    return (
      <div class="home">
        <h1>Home</h1>
        <p class="muted">Cockpit unavailable: {now.error?.message || "no data"}</p>
        <button class="btn" onClick={() => now.refresh()}>Retry</button>
      </div>
    );
  }

  const data = now.data;
  const s = data.state;
  const pnl = report.data?.pnl;

  // Today's running cost (net): realised_net_cost_gbp if present, else fall back to realised_cost_gbp
  const todayNet = pnl?.realised_net_cost_gbp ?? pnl?.realised_cost_gbp ?? null;
  const todaySvt = pnl?.svt_shadow_gbp ?? null;
  const todayDelta =
    pnl?.delta_vs_svt_real_gbp ??
    pnl?.delta_vs_svt_gbp ??
    (todaySvt != null && todayNet != null ? todaySvt - todayNet : null);

  const heroToneClass =
    todayNet == null
      ? "home-hero-cost-neutral"
      : todayNet < 0
        ? "home-hero-cost-positive"
        : "home-hero-cost-neutral";

  // Savings — this month is last entry of monthly history
  const thisMonth = monthly.data[monthly.data.length - 1];
  const monthSavings = thisMonth?.savings_vs_svt_gbp ?? null;
  const monthCost = thisMonth?.cost_gbp ?? null;

  return (
    <div class="home">
      {/* HERO — Today's running cost */}
      <section class="home-hero" aria-label="Today">
        <div>
          <div class="home-hero-eyebrow">
            <strong>Today</strong> · running net cost
          </div>
          <div class={`home-hero-cost ${heroToneClass}`}>
            {todayNet != null ? gbp(todayNet) : "—"}
          </div>
          {todayDelta != null && (
            <div class="home-hero-delta">
              {todaySvt != null && <>vs SVT {gbp(todaySvt)} — </>}
              <strong class={todayDelta >= 0 ? "" : "neg"}>
                {todayDelta >= 0 ? "saved" : "lost"} {gbp(Math.abs(todayDelta))}
              </strong>
              {" so far"}
            </div>
          )}
        </div>
        <aside class="home-hero-aside">
          <div class="home-hero-aside-row">
            <span class="home-hero-aside-label">This month</span>
            <span class="home-hero-aside-value">
              {monthCost != null ? gbp(monthCost) : "—"}
              {monthSavings != null && (
                <span class="muted" style="margin-left:0.5rem; font-size:var(--font-sm)">
                  ({gbpSigned(monthSavings)})
                </span>
              )}
            </span>
          </div>
          <div class="home-hero-aside-row">
            <span class="home-hero-aside-label">Active mode</span>
            <span class="home-hero-aside-value">{s.daikin_mode || "—"}</span>
          </div>
          <div class="home-hero-aside-row">
            <span class="home-hero-aside-label">Current slot</span>
            <span class="home-hero-aside-value">
              {data.current_slot.price_import_p.toFixed(1)}p / {data.current_slot.fox_mode}
            </span>
          </div>
        </aside>
      </section>

      {/* Tariff strip */}
      <Card title="Today's tariff" subtitle="Half-hourly Octopus Agile import prices. Marker = current slot.">
        <TariffStrip
          agile={agile.data}
          cheapP={data.thresholds?.cheap_p ?? 12}
          peakP={data.thresholds?.peak_p ?? 28}
          nowUtc={data.now_utc}
        />
      </Card>

      {/* Live tiles */}
      <section class="home-live" aria-label="Live state">
        <Tile icon="🔋" label="Battery" value={pct(s.soc_pct, 0)} sub={`${kwh(s.soc_kwh)}`} />
        <Tile icon="☀" label="Solar" value={kw(s.solar_kw)} sub={s.solar_kw > 0.1 ? "producing" : "off"} subTone={s.solar_kw > 0.1 ? "ok" : "mute"} />
        <Tile
          icon="⚡"
          label="Grid"
          value={kw(Math.abs(s.grid_kw))}
          sub={s.grid_kw > 0.05 ? "importing" : s.grid_kw < -0.05 ? "exporting" : "idle"}
          subTone={s.grid_kw > 0.05 ? "bad" : s.grid_kw < -0.05 ? "ok" : "mute"}
        />
        <Tile icon="🏠" label="House" value={kw(s.load_kw)} sub="consuming" subTone="mute" />
        <Tile icon="♨" label="Tank" value={tempC(s.tank_c, 0)} sub={s.indoor_c != null ? `indoor ${tempC(s.indoor_c, 0)}` : ""} />
      </section>

      {/* Savings */}
      <Card title="Savings vs SVT" subtitle="How Octopus Agile + battery + solar dispatch compares against the variable tariff.">
        <div class="home-savings">
          <div class="home-savings-headline">
            <div class="home-savings-headline-value">
              {monthSavings != null ? gbpSigned(monthSavings) : "—"}
            </div>
            <div class="home-savings-headline-label">This month vs SVT</div>
          </div>
          <div class="home-savings-aside">
            <SavingsSparkline monthly={monthly.data} />
            {monthly.data.length > 0 && (
              <div class="home-savings-aside-row">
                Last <strong>{monthly.data.length}</strong> months tracked
              </div>
            )}
          </div>
        </div>
      </Card>

      {/* Coming up */}
      <Card title="Coming up" subtitle="The next interesting things on today's price + solar horizon.">
        <ComingUp
          agile={agile.data}
          weather={weather.data}
          cheapP={data.thresholds?.cheap_p ?? 12}
          peakP={data.thresholds?.peak_p ?? 28}
          nowUtc={data.now_utc}
        />
      </Card>

      {monthly.loading && (
        <div class="muted" style="text-align:center; font-size:var(--font-xs)">Loading monthly history…</div>
      )}
    </div>
  );
}

type TileTone = "ok" | "bad" | "warn" | "mute";
function Tile({ icon, label, value, sub, subTone }: { icon: string; label: string; value: string; sub?: string; subTone?: TileTone }) {
  const toneClass = subTone ? `home-live-tile-sub--${subTone}` : "";
  return (
    <div class="home-live-tile">
      <div class="home-live-tile-icon">{icon}</div>
      <div class="home-live-tile-label">{label}</div>
      <div class="home-live-tile-value">{value}</div>
      {sub && <div class={`home-live-tile-sub ${toneClass}`}>{sub}</div>}
    </div>
  );
}
