import { useFetch } from "../../lib/poll";
import { getCosyScorecard } from "../../lib/endpoints";
import type { CosyScoreRow } from "../../lib/types";

// "Cosy scorecard" — the nightly (07:30) server-side score of each day: did the
// house buy at the cheap band and stay off the peak, was the load forecast
// right, was it comfortable, did the hardware behave. Read-only; one row per
// local day, newest first. Details per band live in the row tooltip.

const kwh = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(1));
const gbp = (v: number | null | undefined) => (v == null ? "—" : `£${v.toFixed(2)}`);
const SCORE_LABEL: Record<string, string> = { ideal: "Ideal", below: "Below usual", above: "Above usual" };

function peakError(r: CosyScoreRow): number | null {
  const pk = (r.bands ?? []).filter((b) => b.is_peak && b.load_error_kwh != null);
  return pk.length ? pk.reduce((a, b) => a + (b.load_error_kwh ?? 0), 0) : null;
}

function flags(r: CosyScoreRow): string[] {
  const f: string[] = [];
  if ((r.comfort?.hours_below_night_floor ?? 0) > 0) f.push(`night cold ${r.comfort?.hours_below_night_floor}h`);
  if ((r.comfort?.hours_below_peak_floor ?? 0) > 0) f.push(`peak cold ${r.comfort?.hours_below_peak_floor}h`);
  if (r.tank?.any_below_floor) f.push("tank low");
  if ((r.lwt?.write_verify?.mismatch ?? 0) > 0) f.push("Daikin mismatch");
  if ((r.ops?.fox_failures ?? 0) > 0) f.push("Fox failures");
  return f;
}

export function CosyScorecardCard() {
  const res = useFetch(() => getCosyScorecard(14), [], { cacheKey: "cosy-scorecard", track: true });
  const rows = res.data?.rows ?? [];
  const maxP = Math.max(1, ...rows.map((r) => Math.max(r.avg_import_p ?? 0, r.ideal_avg_import_p ?? 0)));

  return (
    <section class={`cosyscore${res.loading && res.data ? " is-updating" : ""}`}>
      <header class="cosyscore-head">
        <h2>Cosy scorecard</h2>
        <span class="muted">last {rows.length || 14} days · scored 07:30</span>
      </header>
      {rows.length === 0 ? (
        <p class="muted insights-empty">No scored days yet.</p>
      ) : (
        <div class="cosyscore-scroll">
          <table class="cosyscore-table">
            <thead>
              <tr>
                <th>Day</th>
                <th>Score</th>
                <th class="num">Import kWh</th>
                <th class="num">Peak kWh</th>
                <th>Avg p vs ideal</th>
                <th class="num">Net</th>
                <th class="num">Peak fcst err</th>
                <th>Flags</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const pe = peakError(r);
                const fl = flags(r);
                return (
                  <tr key={r.date}>
                    <td>{r.date.slice(5)}</td>
                    <td>
                      {r.score ? (
                        <span class={`cosyscore-pill cosyscore-pill--${r.score}`}>{SCORE_LABEL[r.score]}</span>
                      ) : (
                        <span class="muted">—</span>
                      )}
                    </td>
                    <td class="num">{kwh(r.import_kwh)}</td>
                    <td class={`num${(r.peak_import_kwh ?? 0) > 0.1 ? " cosyscore-warn" : ""}`}>{kwh(r.peak_import_kwh)}</td>
                    <td>
                      <div class="cosyscore-bars" title={`avg ${r.avg_import_p ?? "—"}p, ideal ${r.ideal_avg_import_p ?? "—"}p`}>
                        <span class="cosyscore-bar" style={{ width: `${((r.avg_import_p ?? 0) / maxP) * 100}%` }} />
                        <span class="cosyscore-ideal" style={{ left: `${((r.ideal_avg_import_p ?? 0) / maxP) * 100}%` }} />
                      </div>
                      <span class="cosyscore-bar-label">
                        {r.avg_import_p == null ? "—" : r.avg_import_p.toFixed(1)}p / {r.ideal_avg_import_p == null ? "—" : r.ideal_avg_import_p.toFixed(1)}p
                      </span>
                    </td>
                    <td class="num">{gbp(r.net_cost_gbp)}</td>
                    <td class={`num${r.peak_under_forecast ? " cosyscore-warn" : ""}`}>
                      {pe == null ? "—" : `${pe >= 0 ? "+" : "−"}${Math.abs(pe).toFixed(1)}`}
                    </td>
                    <td>{fl.length ? fl.join(" · ") : <span class="muted">ok</span>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
