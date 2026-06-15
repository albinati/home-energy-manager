import { useEffect, useRef, useState } from "preact/hooks";
import { useFetch } from "../../lib/poll";
import { getResidualProfile, type ResidualProfile, type ResidualProfileSlot } from "../../lib/endpoints";
import { periodWindow, periodLabel, isCurrentPeriod, type PeriodState } from "../../lib/period";
import { makeChart, chartTheme, type EChartsType } from "../../lib/charts";
import { Spinner } from "../common/Spinner";

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

type View = "home" | "hp" | "tank" | "heating";

const HP_VIEWS = new Set<View>(["hp", "tank", "heating"]);

const VIEW_DESC: Record<View, string> = {
  home: "Median household load (heat pump excluded) by day-of-week and hour — the typical at-home pattern the optimizer plans against.",
  hp: "Median heat-pump (Daikin) load by day-of-week and hour — the split subtracted from the household total.",
  tank: "Median hot-water (DHW tank) heat-pump load by day-of-week and hour — measured from the Onecta meters.",
  heating: "Median space-heating heat-pump load by day-of-week and hour — measured from the Onecta meters.",
};

/** Per-(day-of-week × hour) median load heatmap. Views: residual household load
 *  (heat pump excluded — the profile the LP plans against, #477), the combined
 *  heat-pump (Daikin) split it subtracts, and that split broken into hot-water
 *  (tank/DHW) vs space heating from the measured Onecta meters (#574). */
export function LoadPatternCard({ period }: { period: PeriodState }) {
  const { windowDays, endDate } = periodWindow(period);
  // Anchored (month/year) past windows are immutable; the live recent window
  // (day/week, no endDate) keeps refreshing.
  const res = useFetch<ResidualProfile>(
    () => getResidualProfile({ windowDays, endDate }),
    [windowDays, endDate],
    {
      cacheKey: `residual:${windowDays}:${endDate ?? "live"}`,
      immutable: !!endDate && !isCurrentPeriod(period),
      track: true,
    },
  );
  const data = res.data;
  const hasHp = !!data?.hp_by_dow;
  // Tank/Heating need the measured split series specifically — an older backend
  // image can return hp_by_dow without them; gating on hp_dhw_by_dow stops the
  // toggle from mislabelling Home data as Tank/Heating via the fallback.
  const hasSplit = !!data?.hp_dhw_by_dow;
  const [view, setView] = useState<View>("home");
  const elRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);

  useEffect(() => {
    if (!elRef.current || !data) return;
    if (!chartRef.current) chartRef.current = makeChart(elRef.current);
    const t = chartTheme();

    const hpSource: Record<View, Record<string, ResidualProfileSlot[]> | undefined> = {
      home: data.by_dow,
      hp: data.hp_by_dow,
      tank: data.hp_dhw_by_dow,
      heating: data.hp_space_by_dow,
    };
    const source: Record<string, ResidualProfileSlot[]> = hpSource[view] ?? data.by_dow;

    // 7 dow × 24 hours; value = mean of the two half-hour medians in the hour.
    const cells: [number, number, number][] = [];
    let max = 0;
    for (let d = 0; d < 7; d++) {
      const slots = source[String(d)] || [];
      for (let h = 0; h < 24; h++) {
        const a = slots[h * 2]?.median ?? 0;
        const b = slots[h * 2 + 1]?.median ?? 0;
        const v = (a + b) / 2;
        if (v > max) max = v;
        cells.push([h, d, Number(v.toFixed(3))]);
      }
    }

    // Heat-pump view gets a distinct (cool→warm) ramp so the two reads don't
    // get confused; residual keeps the established amber→red.
    const ramp = HP_VIEWS.has(view)
      ? [t.bg ?? "#1b1f27", "#38bdf8", "#ef4444"]
      : [t.bg ?? "#1b1f27", t.pv ?? "#fbbf24", t.importColor ?? "#ef4444"];

    chartRef.current.setOption({
      backgroundColor: "transparent",
      tooltip: {
        position: "top",
        formatter: (p: { value: [number, number, number] }) =>
          `${DOW[p.value[1]]} ${String(p.value[0]).padStart(2, "0")}:00 — <b>${p.value[2].toFixed(2)} kWh</b>/slot`,
      },
      grid: { left: 36, right: 12, top: 8, bottom: 30 },
      xAxis: {
        type: "category",
        data: Array.from({ length: 24 }, (_, h) => (h % 3 === 0 ? String(h) : "")),
        axisLine: { lineStyle: { color: t.border } },
        axisTick: { show: false },
        axisLabel: { color: t.textMute, fontSize: 10 },
      },
      yAxis: {
        type: "category",
        data: DOW,
        axisLine: { lineStyle: { color: t.border } },
        axisTick: { show: false },
        axisLabel: { color: t.textMute, fontSize: 10 },
      },
      visualMap: {
        min: 0, max: Math.max(0.4, max), calculable: false, show: false,
        inRange: { color: ramp },
      },
      series: [{
        type: "heatmap", data: cells,
        itemStyle: { borderColor: "transparent", borderWidth: 0 },
        emphasis: { itemStyle: { borderColor: t.text, borderWidth: 1 } },
        progressive: 0,
      }],
    }, { notMerge: true });
    chartRef.current.resize();
  }, [data, view]);

  useEffect(() => () => { chartRef.current?.dispose(); chartRef.current = null; }, []);

  const dc = data?.day_counts ?? {};
  return (
    <section class={`insights-card load-pattern${res.loading && data ? " is-updating" : ""}`}>
      <header class="load-pattern-head">
        <div class="load-pattern-titlerow">
          <h2>When you spend the most</h2>
          {hasHp && (
            <div class="load-pattern-toggle" role="group" aria-label="Load view">
              {([
                ["home", "Home"],
                ["hp", "Heat pump"],
                ...(hasSplit ? ([["tank", "Tank"], ["heating", "Heating"]] as [View, string][]) : []),
              ] as [View, string][]).map(([v, label]) => (
                <button
                  key={v}
                  class={view === v ? "is-active" : ""}
                  onClick={() => setView(v)}
                  type="button"
                >{label}</button>
              ))}
            </div>
          )}
        </div>
        <p class="muted">{VIEW_DESC[view]}</p>
      </header>
      {res.loading && !data && <Spinner label="Loading load pattern…" />}
      {res.error && <p class="insights-error">Couldn't load the pattern: {res.error.message}</p>}
      {data && (
        <>
          <div ref={elRef} class="load-pattern-chart" />
          <p class="muted load-pattern-meta">
            {endDate ? `${windowDays}-day pattern to ${periodLabel(period)}` : `recent ${windowDays}-day pattern`}
            {" · "}
            {(dc.weekday ?? 0) + (dc.weekend ?? 0)} days learned
            ({dc.weekday ?? 0} weekday / {dc.weekend ?? 0} weekend)
            {(dc.away_excluded ?? 0) > 0 && <> · {dc.away_excluded} away day(s) excluded</>}
            {(dc.negative_excluded ?? 0) > 0 && <> · {dc.negative_excluded} negative-price slot(s) excluded</>}
            {" · "}
            {data.calibrated_days}/{data.calibrated_days + data.physics_only_days} days calibrated
            to the measured heat-pump split
            {data.away_days.length > 0 && (
              <> · away: {data.away_days.slice(-5).join(", ")}{data.away_days.length > 5 ? " …" : ""}</>
            )}
          </p>
        </>
      )}
    </section>
  );
}
