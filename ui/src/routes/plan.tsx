import { useEffect, useMemo, useState } from "preact/hooks";
import { useFetch } from "../lib/poll";
import {
  getAgileToday,
  getAgileDay,
  getOctopusConsumption,
  getSchedulerTimeline,
  getDecisionsLatest,
  getWeather,
  getPvCalibration,
} from "../lib/endpoints";
import { Widget } from "../components/common/Widget";
import { Pill } from "../components/common/Pill";
import { Spinner } from "../components/common/Spinner";
import { Icon } from "../components/common/Icon";
import { DispatchPlanStrip } from "../components/forecast/DispatchPlanStrip";
import { RatesChart } from "../components/plan/RatesChart";
import { SevenDayBar } from "../components/plan/SevenDayBar";
import { useAnimatedNumber } from "../lib/useAnimatedNumber";
import { pence, pct } from "../lib/format";
import type {
  AgileDaySlotsResponse,
  OctopusConsumptionResponse,
} from "../lib/types";
import "../components/forecast/forecast.css";
import "../components/plan/plan.css";

const CHEAP_FALLBACK = 12;
const PEAK_FALLBACK = 28;

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export default function Plan() {
  const today = new Date();
  const yesterday = new Date(today); yesterday.setDate(today.getDate() - 1);
  const tomorrow = new Date(today); tomorrow.setDate(today.getDate() + 1);

  const agileToday = useFetch(getAgileToday, []);
  const agileTomorrow = useFetch<AgileDaySlotsResponse | null>(
    () => getAgileDay(isoDate(tomorrow)).catch(() => null),
    [],
  );
  const agileYesterday = useFetch(() => getAgileDay(isoDate(yesterday)), []);
  const consumption = useFetch(getOctopusConsumption, []);
  const timeline = useFetch(getSchedulerTimeline, []);
  const decisions = useFetch(getDecisionsLatest, []);
  const weather = useFetch(getWeather, []);
  const cal = useFetch(getPvCalibration, []);
  const sevenDay = useSevenDayHistory();

  const cheapP = CHEAP_FALLBACK;
  const peakP = PEAK_FALLBACK;

  const consumptionByStart = useMemo(
    () => buildConsumptionMap(consumption.data, isoDate(yesterday)),
    [consumption.data, yesterday.getTime()],
  );

  const todayStats = priceStats(agileToday.data?.import_slots ?? []);
  const tomorrowStats = priceStats(agileTomorrow.data?.slots ?? []);
  const yesterdayStats = priceStats(agileYesterday.data?.slots ?? []);
  const yesterdayCost = computeYesterdayCost(agileYesterday.data, consumptionByStart);

  // Focal number for this surface: predicted end-of-horizon SoC — the LAST
  // finite soc_percent already in the timeline (no recompute, no new fetch).
  const tl = timeline.data;
  const planSlots = tl
    ? [...(tl.executed || []), ...(tl.ongoing ? [tl.ongoing] : []), ...(tl.planned || [])]
    : [];
  let endSoc: number | null = null;
  for (const s of planSlots) {
    if (s.soc_percent != null && Number.isFinite(s.soc_percent)) endSoc = s.soc_percent;
  }
  const endSocAnim = useAnimatedNumber(endSoc);

  return (
    <div class="plan-page">
      <header class="plan-header">
        <div>
          <div class="plan-eyebrow">Deep dive</div>
          <h1>Plan & rates</h1>
          <p class="plan-sub">
            What the LP intends to do, how Octopus Agile is pricing the day, what
            we actually consumed yesterday, and how this week compares.
          </p>
        </div>
        {cal.data?.factor != null && (
          <Pill tone={Math.abs(cal.data.factor - 1) < 0.1 ? "ok" : "warn"}>
            <span class="plan-pill-icon"><Icon name="efficiency" size={14} /></span>
            PV cal {cal.data.factor.toFixed(2)}×
          </Pill>
        )}
      </header>

      <div class="widget-grid">
        <Widget
          title="The plan — next 48 h"
          icon={<Icon name="schedule" size={18} />}
          tone="plan"
          size="wide"
          badge={timeline.data?.plan_date
            ? `${timeline.data.plan_date} · ${timeline.data.run_at?.slice(11, 16) || "—"} UTC`
            : undefined}
        >
          {endSoc != null && (
            <div class="plan-focal">
              <span class="plan-focal-icon"><Icon name="battery" size={20} /></span>
              <div class="plan-focal-num">
                {endSocAnim != null ? pct(endSocAnim, 0) : "—"}
              </div>
              <div class="plan-focal-label">predicted battery<br />at end of horizon</div>
            </div>
          )}
          {timeline.loading ? <Spinner label="Loading plan…" /> : <DispatchPlanStrip timeline={timeline.data} decisions={decisions.data} />}
        </Widget>

        <Widget
          title="Today's rates"
          icon={<Icon name="cost" size={18} />}
          tone="tariff"
          size="wide"
          badge={todayStats ? `min ${pence(todayStats.min)} · avg ${pence(todayStats.avg)} · peak ${pence(todayStats.max)}` : undefined}
        >
          {agileToday.loading ? (
            <Spinner label="Loading today's rates…" />
          ) : agileToday.data ? (
            <RatesChart
              importSlots={agileToday.data.import_slots}
              exportSlots={agileToday.data.export_slots}
              cheapP={cheapP}
              peakP={peakP}
            />
          ) : <p class="muted">No data.</p>}
        </Widget>

        <Widget
          title="Tomorrow's rates"
          icon={<Icon name="cost" size={18} />}
          tone="tariff"
          size="wide"
          badge={tomorrowStats
            ? `min ${pence(tomorrowStats.min)} · avg ${pence(tomorrowStats.avg)} · peak ${pence(tomorrowStats.max)}`
            : "Awaiting Octopus"}
        >
          {agileTomorrow.loading ? (
            <Spinner label="Checking tomorrow…" />
          ) : agileTomorrow.data?.slots && agileTomorrow.data.slots.length > 0 ? (
            <RatesChart
              importSlots={agileTomorrow.data.slots}
              cheapP={cheapP}
              peakP={peakP}
            />
          ) : (
            <div class="plan-empty">
              <strong>Not published yet.</strong> Tomorrow's Agile rates land around 16:00 local once Octopus releases them.
            </div>
          )}
        </Widget>

        <Widget
          title="Yesterday + what we actually used"
          icon={<Icon name="chart-bars" size={18} />}
          tone="savings"
          size="wide"
          badge={yesterdayCost != null
            ? `realised ~ £${yesterdayCost.toFixed(2)}`
            : yesterdayStats
              ? `min ${pence(yesterdayStats.min)} · avg ${pence(yesterdayStats.avg)} · peak ${pence(yesterdayStats.max)}`
              : undefined}
        >
          {agileYesterday.loading ? (
            <Spinner label="Loading yesterday…" />
          ) : agileYesterday.data ? (
            <RatesChart
              importSlots={agileYesterday.data.slots}
              consumptionByStart={consumptionByStart}
              cheapP={cheapP}
              peakP={peakP}
            />
          ) : <p class="muted">No data.</p>}
        </Widget>

        <Widget
          title="Last 7 days · daily mean"
          icon={<Icon name="trend" size={18} />}
          tone="tariff"
          size="large"
          badge="min/max whisker"
        >
          {sevenDay.loading ? (
            <Spinner label="Loading week…" />
          ) : sevenDay.data.length === 0 ? (
            <p class="muted">No history loaded.</p>
          ) : (
            <SevenDayBar days={sevenDay.data} />
          )}
        </Widget>

        <Widget
          title="Solar forecast"
          icon={<Icon name="solar" size={18} />}
          tone="default"
          size="medium"
          badge="48 h"
        >
          {weather.loading || !weather.data ? (
            <Spinner label="Loading forecast…" />
          ) : (
            <PvSparkline data={weather.data.forecast} />
          )}
        </Widget>
      </div>
    </div>
  );
}

function useSevenDayHistory() {
  const [data, setData] = useState<AgileDaySlotsResponse[]>([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    const dates: string[] = [];
    for (let i = 6; i >= 0; i--) {
      const d = new Date();
      d.setDate(d.getDate() - i);
      dates.push(isoDate(d));
    }
    Promise.all(dates.map((d) => getAgileDay(d).catch(() => null))).then((rs) => {
      if (!alive) return;
      setData(rs.filter((r): r is AgileDaySlotsResponse => !!r));
      setLoading(false);
    });
    return () => { alive = false; };
  }, []);
  return { data, loading };
}

function priceStats(slots: { p: number }[]): { min: number; max: number; avg: number } | null {
  if (slots.length === 0) return null;
  let mn = Infinity, mx = -Infinity, sum = 0;
  for (const s of slots) {
    if (s.p < mn) mn = s.p;
    if (s.p > mx) mx = s.p;
    sum += s.p;
  }
  return { min: mn, max: mx, avg: sum / slots.length };
}

function buildConsumptionMap(
  data: OctopusConsumptionResponse | null,
  yesterdayIso: string,
): Map<string, number> {
  const map = new Map<string, number>();
  if (!data?.slots) return map;
  for (const s of data.slots) {
    const localDate = s.interval_start.slice(0, 10);
    if (localDate === yesterdayIso) {
      map.set(s.interval_start, s.consumption_kwh);
    }
  }
  return map;
}

function computeYesterdayCost(
  agile: AgileDaySlotsResponse | null,
  consumptionByStart: Map<string, number>,
): number | null {
  if (!agile?.slots || consumptionByStart.size === 0) return null;
  let totalP = 0;
  let matched = 0;
  for (const slot of agile.slots) {
    const ms = Date.parse(slot.valid_from);
    for (const [iso, kwh] of consumptionByStart) {
      if (Date.parse(iso) === ms) {
        totalP += slot.p * kwh;
        matched++;
        break;
      }
    }
  }
  return matched > 0 ? totalP / 100 : null;
}

interface ForecastSlot { time: string; pv_kw: number; temp_c: number; }
function PvSparkline({ data }: { data: ForecastSlot[] }) {
  if (!data || data.length === 0) return <p class="muted">No forecast.</p>;
  const w = 800, h = 100;
  const max = Math.max(0.1, ...data.map((d) => d.pv_kw));
  const stepX = w / (data.length - 1 || 1);
  const points = data.map((d, i) => `${(i * stepX).toFixed(1)},${(h - (d.pv_kw / max) * (h - 4)).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style="width:100%; height:110px">
      <defs>
        <linearGradient id="pv-spark-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="var(--pv)" stop-opacity="0.20" />
          <stop offset="100%" stop-color="var(--pv)" stop-opacity="0" />
        </linearGradient>
      </defs>
      <line x1="0" y1={h - 0.5} x2={w} y2={h - 0.5} stroke="var(--border)" stroke-width="1" vector-effect="non-scaling-stroke" />
      <polygon points={`0,${h} ${points} ${w},${h}`} fill="url(#pv-spark-fill)" />
      <polyline points={points} fill="none" stroke="var(--pv)" stroke-width="2" vector-effect="non-scaling-stroke" />
    </svg>
  );
}
