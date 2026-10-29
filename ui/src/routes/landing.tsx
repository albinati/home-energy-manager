import { useEffect } from "preact/hooks";
import { usePoll } from "../lib/poll";
import { getCockpitNow, getPlanFronts, getStatusAlerts } from "../lib/endpoints";
import { Icon } from "../components/common/Icon";
import { Spinner } from "../components/common/Spinner";
import { computeChips, type Chip } from "../components/shell/AlertStrip";
import { StatusLine, AgoraLine } from "../components/home/simple/StatusLine";
import { Tile } from "../components/home/simple/Tile";
import { MoneyCard } from "../components/home/simple/MoneyCard";
import { ModeBox } from "../components/home/simple/ModeBox";
import {
  buildStatus, casaTile, aguaTile, bateriaTile, agoraParts, agoraStale, holidayFlags, footerLine,
} from "../components/home/simple/logic";
import { publishFreshness, publishCockpitConn } from "../lib/freshness";
import { buildSha } from "../lib/api";
import { role } from "../lib/auth";
import { plannerTz } from "../lib/plannerTz";
import "../styles/home-simple.css";

// Dev-only fixture switch (`?mock=1`); eliminated from the production bundle.
const MOCK = import.meta.env.DEV && new URLSearchParams(location.search).get("mock") === "1";

// Home (#879): ridiculously simple. Reads ONLY /cockpit/now (10 s),
// /plan/fronts (60 s) and /status/alerts (60 s); /whoami is fetched by the app
// shell. No chart library is imported on this route.
export default function Landing() {
  const now = usePoll(async () => {
    if (import.meta.env.DEV && MOCK) return (await import("../lib/mock/cockpit")).MOCK_COCKPIT;
    return getCockpitNow();
  }, 10_000);
  const fronts = usePoll(async () => {
    if (import.meta.env.DEV && MOCK) return (await import("../lib/mock/planFronts")).MOCK_PLAN_FRONTS;
    return getPlanFronts();
  }, 60_000);
  const alerts = usePoll(getStatusAlerts, 60_000);

  // Keep the shared freshness map fresh for the alert chips (cleared on unmount).
  useEffect(() => { publishFreshness(now.data?.freshness); }, [now.data]);
  useEffect(() => { publishCockpitConn({ failCount: now.failCount, lastFetchAt: now.lastFetchAt }); }, [now.failCount, now.lastFetchAt]);
  useEffect(() => () => { publishFreshness(null); publishCockpitConn(null); }, []);
  useEffect(() => { plannerTz.value = now.data?.planner_tz; }, [now.data?.planner_tz]);

  if (now.loading && !now.data) {
    return <div class="hs"><Spinner label="Carregando…" /></div>;
  }
  if (!now.data) {
    return (
      <div class="hs">
        <p class="hs-muted">Painel indisponível: {now.error?.message || "sem dados"}</p>
        <button class="btn" onClick={() => now.refresh()}>Tentar de novo</button>
      </div>
    );
  }

  const data = now.data;
  const f = fronts.data;
  const nowMs = Date.parse(data.now_utc);
  const holiday = holidayFlags(data);
  // An unreachable /status/alerts must not read as "Tudo normal".
  const chips: Chip[] = alerts.error && !alerts.data
    ? [{ key: "alerts-unavailable", tone: "warn", label: "Status indisponível", detail: alerts.error.message }]
    : computeChips(alerts.data, alerts.error);
  const sha = buildSha();
  const planRunAt = f?.battery && !f.battery.error ? f.battery.plan_run_at : null;

  return (
    <div class="hs">
      <h1 class="sr-only">Casa</h1>
      <StatusLine chips={chips} text={buildStatus({ now: data, fronts: f })} />
      <AgoraLine parts={agoraParts(data)} stale={agoraStale(data)} />

      <div class="hs-tiles">
        <Tile icon={<Icon name="house" size={14} />} title="Casa" t={casaTile(data, f)} />
        <Tile icon={<Icon name="droplet" size={14} />} title="Água quente" t={aguaTile(data, f)} />
        <Tile icon={<Icon name="battery" size={14} />} title="Bateria" t={bateriaTile(data, f)} />
      </div>

      <MoneyCard fronts={f} nowMs={nowMs} holiday={holiday} tz={data.planner_tz} />

      {role.value === "admin" && (
        <ModeBox vacation={data.vacation} preset={data.modes?.optimization_preset}
                 battery={f?.battery} tariff={f?.tariff} nowMs={nowMs} tz={data.planner_tz}
                 onChanged={() => { void now.refresh(); void fronts.refresh(); }} />
      )}

      <p class="hs-footer">{footerLine(data, planRunAt, sha.length > 7 ? sha.slice(0, 7) : sha)}</p>
    </div>
  );
}
