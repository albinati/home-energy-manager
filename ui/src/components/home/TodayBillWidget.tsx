import { useEffect, useState } from "preact/hooks";
import type { EnergyReport, MetricsResponse } from "../../lib/types";
import { gbp, gbpSigned } from "../../lib/format";
import { getEnergyReport } from "../../lib/endpoints";
import { Spinner } from "../common/Spinner";
import "./today-bill.css";

interface TodayBillWidgetProps {
  report: EnergyReport | null;
  reportLoading: boolean;
  metrics: MetricsResponse | null;
}

// Realised today + projected EOD + 30-day average comparison + a
// 7-day bar chart so today reads in context of the week.
export function TodayBillWidget({ report, reportLoading, metrics }: TodayBillWidgetProps) {
  const history = useWeekHistory();

  const pnl = report?.pnl;
  const realised = pnl?.realised_net_cost_gbp ?? pnl?.realised_cost_gbp ?? null;

  const now = new Date();
  const hoursElapsed = Math.max(0.5, now.getHours() + now.getMinutes() / 60);
  const projected = realised != null && hoursElapsed > 0
    ? (realised / hoursElapsed) * 24
    : null;

  // Don't full-blank the widget while /energy/report (~4 s) is in flight —
  // show skeletons inside instead so the widget renders immediately.
  const isLoadingReport = reportLoading && !report;

  const monthDelta = metrics?.pnl?.monthly?.delta_vs_svt_pounds ?? null;
  const dayOfMonth = now.getDate();
  const dma = monthDelta != null ? monthDelta / Math.max(1, dayOfMonth) : null;
  const todayDelta = metrics?.pnl?.daily?.delta_vs_svt_pounds ?? null;
  const dmaCompare = todayDelta != null && dma != null ? todayDelta - dma : null;

  return (
    <div class="today-bill">
      <div class="today-bill-headline">
        <div class="today-bill-realised">
          <span class="today-bill-label">Today so far</span>
          <span class="today-bill-amount today-bill-amount-realised">
            {realised != null
              ? gbp(realised)
              : isLoadingReport
                ? <span class="skel-text" style={{ width: "4rem", height: "1.4rem" }} />
                : "—"}
          </span>
        </div>
        <div class="today-bill-projected">
          <span class="today-bill-label">→ projected EOD</span>
          <span class="today-bill-amount today-bill-amount-projected">
            {projected != null
              ? gbp(projected)
              : isLoadingReport
                ? <span class="skel-text" style={{ width: "4rem", height: "1.4rem" }} />
                : "—"}
          </span>
        </div>
      </div>

      <WeekBars history={history.data} loading={history.loading} todayCost={realised} />

      <div class="today-bill-rows">
        {dma != null && (
          <div class="today-bill-row">
            <span class="today-bill-row-label">30-day avg saving</span>
            <span class="today-bill-row-value">{gbpSigned(dma)}/day</span>
          </div>
        )}
        {dmaCompare != null && (
          <div class="today-bill-row">
            <span class="today-bill-row-label">vs typical day</span>
            <span class={`today-bill-row-value ${dmaCompare >= 0 ? "today-bill-row-value-ok" : "today-bill-row-value-bad"}`}>
              {gbpSigned(dmaCompare)}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

interface DayCost { date: string; cost: number | null; }

function useWeekHistory() {
  const [data, setData] = useState<DayCost[]>([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    const dates: string[] = [];
    for (let i = 6; i >= 0; i--) {
      const d = new Date();
      d.setDate(d.getDate() - i);
      dates.push(d.toISOString().slice(0, 10));
    }
    Promise.all(
      dates.map((d) =>
        getEnergyReport(d)
          .then((r) => ({ date: d, cost: r?.pnl?.realised_net_cost_gbp ?? r?.pnl?.realised_cost_gbp ?? null }))
          .catch(() => ({ date: d, cost: null })),
      ),
    ).then((results) => {
      if (!alive) return;
      setData(results);
      setLoading(false);
    });
    return () => { alive = false; };
  }, []);
  return { data, loading };
}

interface WeekBarsProps {
  history: DayCost[];
  loading: boolean;
  todayCost: number | null;
}

function WeekBars({ history, loading, todayCost }: WeekBarsProps) {
  if (loading && history.length === 0) {
    return (
      <div class="today-bill-week">
        <div class="today-bill-week-label">Last 7 days</div>
        <Spinner size="sm" label="loading…" />
      </div>
    );
  }
  if (history.length === 0) return null;
  // If realised today not in history yet, replace last bar with current realised.
  const todayIso = new Date().toISOString().slice(0, 10);
  const data = history.map((d) => (d.date === todayIso && todayCost != null ? { ...d, cost: todayCost } : d));
  const max = Math.max(0.01, ...data.map((d) => Math.abs(d.cost ?? 0)));

  return (
    <div class="today-bill-week">
      <div class="today-bill-week-header">
        <span class="today-bill-week-label">Last 7 days</span>
        <span class="today-bill-week-total muted">
          {gbp(data.reduce((acc, d) => acc + (d.cost ?? 0), 0))} total
        </span>
      </div>
      <div class="today-bill-week-bars">
        {data.map((d) => {
          const cost = d.cost;
          const h = cost != null ? (Math.abs(cost) / max) * 100 : 0;
          const isToday = d.date === todayIso;
          const label = new Date(d.date + "T12:00:00").toLocaleDateString([], { weekday: "short" });
          const color = cost == null ? "var(--text-mute)" : cost < 0 ? "var(--ok)" : "var(--text)";
          return (
            <div class={`today-bill-week-day${isToday ? " is-today" : ""}`} key={d.date} title={`${d.date}: ${cost != null ? gbp(cost) : "—"}`}>
              <div class="today-bill-week-bar-track">
                <div class="today-bill-week-bar-fill"
                     style={{ height: `${h}%`, background: color, opacity: isToday ? 1 : 0.5 }} />
              </div>
              <div class="today-bill-week-day-label">{label[0]}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
