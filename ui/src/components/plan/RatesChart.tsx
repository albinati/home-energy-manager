import { useEffect, useRef } from "preact/hooks";
import { makeChart, baseOption, chartTheme, areaGradient, type EChartsType } from "../../lib/charts";
import { useResolvedTheme } from "../../lib/theme";
import type { AgileSlot } from "../../lib/types";

interface RatesChartProps {
  importSlots: AgileSlot[];
  exportSlots?: AgileSlot[];
  consumptionByStart?: Map<string, number>;   // ISO of slot start → kWh
  cheapP: number;
  peakP: number;
  height?: number;
}

// Single-day rates bar chart. Import as coloured bars, optional export as a
// dotted line on a second y-axis, optional realised consumption as a line on
// a third axis when provided ("yesterday + actuals" mode).
export function RatesChart({ importSlots, exportSlots, consumptionByStart, cheapP, peakP, height = 220 }: RatesChartProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);
  const theme = useResolvedTheme();

  useEffect(() => {
    if (!ref.current) return;
    const ch = makeChart(ref.current);
    chartRef.current = ch;
    const onResize = () => ch.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      ch.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!chartRef.current) return;
    const t = chartTheme();
    const base = baseOption();
    const sorted = importSlots.slice().sort((a, b) => a.valid_from.localeCompare(b.valid_from));
    const xs = sorted.map((s) => new Date(s.valid_from).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }));

    const importData = sorted.map((s) => {
      const kind = s.kind || (s.p < 0 ? "negative" : s.p < cheapP ? "cheap" : s.p >= peakP ? "peak" : "standard");
      const color = kind === "negative" ? t.neg : kind === "cheap" ? t.cheap : kind === "peak" ? t.peak : t.textDim;
      return { value: s.p, itemStyle: { color, borderRadius: [3, 3, 0, 0] } };
    });

    const exportData = exportSlots
      ? sorted.map((s, i) => {
          const e = exportSlots.find((x) => x.valid_from === s.valid_from);
          return e ? e.p : exportSlots[i]?.p ?? null;
        })
      : null;

    const consumptionData = consumptionByStart
      ? sorted.map((s) => {
          // Match by epoch ms — consumption slot ISO may carry a different TZ suffix
          // than the import slot's UTC suffix.
          const startMs = Date.parse(s.valid_from);
          for (const [iso, kwh] of consumptionByStart) {
            if (Date.parse(iso) === startMs) return kwh;
          }
          return null;
        })
      : null;

    const series: unknown[] = [
      {
        name: "Import",
        type: "bar",
        barWidth: "85%",
        data: importData,
        yAxisIndex: 0,
        markLine: {
          silent: true,
          symbol: "none",
          lineStyle: { color: t.border, type: "dashed", opacity: 0.6 },
          data: [
            { yAxis: peakP, label: { color: t.peak, formatter: `${peakP}p` } },
            { yAxis: cheapP, label: { color: t.cheap, formatter: `${cheapP}p` } },
            { yAxis: 0, label: { color: t.textDim, formatter: "0" } },
          ],
        },
      },
    ];
    if (exportData) {
      series.push({
        name: "Export",
        type: "line",
        smooth: false,
        symbol: "none",
        lineStyle: { color: t.exportColor, width: 1.75 },
        areaStyle: { color: areaGradient(t.exportColor, 0.06, 0.0) },
        yAxisIndex: 0,
        data: exportData,
      });
    }
    if (consumptionData) {
      // Consumption is not a domain — monochrome, not accent (accent stays for
      // interaction only).
      series.push({
        name: "Imported kWh",
        type: "line",
        smooth: false,
        symbol: "circle",
        symbolSize: 3,
        lineStyle: { color: t.textDim, width: 2 },
        itemStyle: { color: t.textDim },
        yAxisIndex: 1,
        data: consumptionData,
      });
    }

    const yAxes: unknown[] = [
      { ...(base.yAxis as object), name: "p/kWh", nameTextStyle: { color: t.textDim, fontSize: 10 } },
    ];
    if (consumptionData) {
      yAxes.push({
        type: "value",
        name: "kWh",
        nameTextStyle: { color: t.textMute, fontSize: 10 },
        position: "right",
        splitLine: { show: false },
        axisLabel: { color: t.textMute, fontSize: 10 },
      });
    }

    chartRef.current.setOption({
      ...base,
      legend: {
        ...(base.legend as object),
        data: ["Import", ...(exportData ? ["Export"] : []), ...(consumptionData ? ["Imported kWh"] : [])],
      },
      xAxis: { ...(base.xAxis as object), data: xs },
      yAxis: yAxes,
      series,
    }, { notMerge: true });
  }, [importSlots, exportSlots, consumptionByStart, cheapP, peakP, theme]);

  return <div ref={ref} style={{ width: "100%", height: `${height}px` }} />;
}
