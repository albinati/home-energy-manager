import { useFetch } from "../../lib/poll";
import { getLwtLearning } from "../../lib/endpoints";
import type { LwtLearningSlot } from "../../lib/types";

// "LWT learning" — how the house actually behaved during LWT coast windows
// (#838): implied UA and pump k per day against the pinned values, plus
// yesterday's predicted vs realised indoor temperature per slot.

const n1 = (v: number | null | undefined, d = 1) => (v == null ? "—" : v.toFixed(d));

function Strip({ slots }: { slots: LwtLearningSlot[] }) {
  const pts = slots.filter((s) => s.indoor_real_c != null || s.indoor_pred_c != null);
  if (pts.length === 0) return <p class="muted insights-empty">No per-slot data for yesterday yet.</p>;
  const vals = pts.flatMap((s) => [s.indoor_real_c, s.indoor_pred_c, s.floor_c].filter((v): v is number => v != null));
  const lo = Math.min(...vals) - 0.2;
  const hi = Math.max(...vals) + 0.2;
  const y = (v: number) => `${((hi - v) / (hi - lo)) * 100}%`;
  return (
    <div class="lwtl-strip" role="img" aria-label="Predicted versus realised indoor temperature, yesterday">
      {pts.map((s) => {
        const coast = (s.device_offset ?? 0) < 0 || (s.offset_written ?? 0) < 0;
        const h = new Date(s.slot_time_utc);
        return (
          <div
            key={s.slot_time_utc}
            class={`lwtl-col${coast ? " lwtl-col--coast" : ""}`}
            title={`${h.toISOString().slice(11, 16)}Z  pred ${n1(s.indoor_pred_c)}  real ${n1(s.indoor_real_c)}  offset ${n1(s.device_offset, 0)}`}
          >
            {s.floor_c != null && <span class="lwtl-floor" style={{ top: y(s.floor_c) }} />}
            {s.indoor_pred_c != null && <span class="lwtl-pred" style={{ top: y(s.indoor_pred_c) }} />}
            {s.indoor_real_c != null && <span class="lwtl-real" style={{ top: y(s.indoor_real_c) }} />}
          </div>
        );
      })}
    </div>
  );
}

export function LwtLearningCard() {
  const res = useFetch(() => getLwtLearning(14), [], { cacheKey: "lwt-learning", track: true });
  const d = res.data;
  const rows = d?.daily ?? [];
  return (
    <section class={`lwtl${res.loading && d ? " is-updating" : ""}`}>
      <header class="lwtl-head">
        <h2>LWT learning</h2>
        <span class="muted">coast mode {d?.coast_mode ?? "—"} · pinned UA {d ? n1(d.ua_pinned_w_per_k, 0) : "—"} W/K</span>
      </header>
      {res.error && !d ? (
        <p class="muted insights-empty lwtl-error">Could not load LWT learning: {res.error.message}</p>
      ) : rows.length === 0 ? (
        <p class="muted insights-empty">No learning days yet (first summary after 04:40 UTC).</p>
      ) : (
        <div class="lwtl-scroll">
          <table class="lwtl-table">
            <thead>
              <tr>
                <th>Day</th>
                <th class="num">Coast slots</th>
                <th class="num">UA night W/K</th>
                <th class="num">UA all-coast W/K</th>
                <th class="num">UA pinned</th>
                <th class="num">k est kW/°C</th>
                <th class="num">k pinned</th>
                <th class="num">Pred err mean °C</th>
                <th class="num">Pred err p90 °C</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.date}>
                  <td>{r.date.slice(5)}</td>
                  <td class="num">{r.n_coast_slots ?? "—"}</td>
                  <td class="num">{n1(r.ua_est_night_w_per_k, 0)}</td>
                  <td class="num">{n1(r.ua_est_w_per_k, 0)}</td>
                  <td class="num">{n1(r.ua_pinned_w_per_k, 0)}</td>
                  <td class="num">{n1(r.k_est_kw_per_c, 3)}</td>
                  <td class="num">{n1(r.k_pinned_kw_per_c, 3)}</td>
                  <td class="num">{n1(r.pred_err_mean_c, 2)}</td>
                  <td class="num">{n1(r.pred_err_p90_c, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <h3 class="lwtl-sub">Yesterday {d?.yesterday.date.slice(5) ?? ""}: predicted vs realised indoor</h3>
      <Strip slots={d?.yesterday.slots ?? []} />
      <p class="muted lwtl-legend">
        <span class="lwtl-key lwtl-key--pred" /> predicted <span class="lwtl-key lwtl-key--real" /> realised{" "}
        <span class="lwtl-key lwtl-key--floor" /> comfort floor · shaded = coast slot
      </p>
    </section>
  );
}
