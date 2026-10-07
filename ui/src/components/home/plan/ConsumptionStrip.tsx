import type { PlanFrontsConsumption } from "../../../lib/types";
import { num, range } from "./util";
import "./plan.css";

export function ConsumptionStrip({ data, loading }: { data: PlanFrontsConsumption | null; loading: boolean }) {
  if (!data) {
    return loading ? <div class="cs"><span class="skel-text" style={{ width: "100%", height: "6em" }} /></div>
      : <p class="muted">Consumption outlook unavailable.</p>;
  }
  const bands = data.bands ?? [];
  if (data.error || !bands.length) return <p class="muted">Consumption outlook unavailable{data.error ? ` (${data.error})` : ""}.</p>;

  const scale = Math.max(0.1, ...bands.flatMap((b) => [
    b.expected_kwh?.p90 ?? 0, b.committed_kwh ?? 0, b.realised_kwh ?? 0,
  ]));
  const pct = (v: number | null | undefined) => `${Math.min(100, Math.max(0, ((v ?? 0) / scale) * 100))}%`;
  const day = data.day;
  const dayExp = day?.expected_kwh;
  return (
    <div class="cs">
      {bands.map((b) => {
        const e = b.expected_kwh ?? { p50: null, p75: null, p90: null, n_days: 0 };
        const peak = b.key === "band_peak" || b.label === "peak";
        return (
          <div key={`${b.key}-${b.start_local}`} class={`cs-row cs-row--${b.status}`}>
            <div class="cs-label">
              <b>{b.label}</b>
              <span>{range(b.start_local, b.end_local)} · {num(b.price_p, 1)}p</span>
            </div>
            <div class="cs-bar">
              <div class="cs-track" role="img"
                   aria-label={`${b.label} band: expected p50 ${num(e.p50)} p75 ${num(e.p75)} p90 ${num(e.p90)} kWh`}>
                <div class="cs-p90" style={{ width: pct(e.p90) }} />
                <div class="cs-p75" style={{ width: pct(e.p75) }} />
                <div class="cs-p50" style={{ width: pct(e.p50) }} />
                {b.realised_kwh != null && <div class="cs-real" style={{ width: pct(b.realised_kwh) }} />}
                {b.committed_kwh != null && <div class="cs-tick" style={{ left: pct(b.committed_kwh) }} title={`committed ${num(b.committed_kwh)} kWh`} />}
              </div>
              <div class="cs-meta">
                <span>p50 <b>{num(e.p50)}</b></span>
                <span>p75 <b>{num(e.p75)}</b></span>
                <span>p90 <b>{num(e.p90)}</b> kWh</span>
                {b.committed_kwh != null && <span>plan <b>{num(b.committed_kwh)}</b></span>}
                {b.realised_kwh != null && <span>so far <b>{num(b.realised_kwh)}</b></span>}
                {peak && (b.forecast_error_kwh?.n_days ?? 0) > 0 && (
                  <span title="Days the realised band load exceeded the committed forecast">
                    under-forecast <b>{b.forecast_error_kwh?.under_forecast_days}/{b.forecast_error_kwh?.n_days}</b>
                  </span>
                )}
              </div>
            </div>
          </div>
        );
      })}
      <div class="cs-legend">
        <span><span class="cs-sw" style={{ background: "color-mix(in srgb, var(--house) 14%, var(--bg-card-3))" }} />p90</span>
        <span><span class="cs-sw" style={{ background: "var(--house)" }} />realised</span>
        <span><span class="cs-sw cs-sw--tick" />committed</span>
        {dayExp?.p50 != null && (
          <span>day p50 {num(dayExp.p50)} · p90 {num(dayExp.p90)} kWh
            {day?.realised_kwh != null && <> · so far {num(day.realised_kwh)}</>}
            {" "}· {dayExp.n_days} same-day-type days</span>
        )}
      </div>
    </div>
  );
}
