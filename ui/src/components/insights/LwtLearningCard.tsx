import { useFetch } from "../../lib/poll";
import { getLwtLearning, getThermalCalibration } from "../../lib/endpoints";
import type { LwtLearningSlot } from "../../lib/types";

// "LWT learning" — how the house actually behaved during LWT coast windows
// (#838): implied UA and pump k per day against the pinned values, plus
// yesterday's predicted vs realised indoor temperature per slot.

const localHM = (iso: string, tz: string | undefined) => {
  try {
    return new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: tz }).format(
      new Date(iso),
    );
  } catch {
    return iso.slice(11, 16);
  }
};

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
  const cal = useFetch(() => getThermalCalibration(), [], { cacheKey: "thermal-calibration-lwtl" });
  const eff = cal.data?.effective;
  const rows = d?.daily ?? [];
  const latest = rows.find((r) => r.joint_fit) ?? rows[0];
  const joint = latest?.joint_fit ?? null;
  const bands = latest?.night_rise_per_band ?? [];
  const tz = d?.timezone;
  return (
    <section class={`lwtl${res.loading && d ? " is-updating" : ""}`}>
      <header class="lwtl-head">
        <h2>LWT learning</h2>
        <span class="muted">coast mode {d?.coast_mode ?? "—"} · pinned UA {d ? n1(d.ua_pinned_w_per_k, 0) : "—"} W/K</span>
      </header>
      {eff && (
        <p class="muted lwtl-thermal">
          Thermal model ({eff.source}): τ {n1(eff.tau_hours, 1)} h · UA {n1(eff.ua_w_per_k, 0)} W/K · C{" "}
          {n1(eff.c_kwh_per_k, 1)} kWh/K
          {eff.c_basis_ua_w_per_k != null ? ` (stored C basis UA ${n1(eff.c_basis_ua_w_per_k, 0)} W/K)` : ""}
          {eff.c_recomputed ? " · recomputed as τ × UA" : ""}
        </p>
      )}
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
      {joint && (
        <div class="lwtl-joint">
          <h3 class="lwtl-sub">UA / C episode fit (last {latest?.joint_window_days ?? 14} days)</h3>
          {joint.identifiable ? (
            <p>
              UA {n1(joint.ua_w_per_k, 0)} ± {n1(joint.ua_se, 0)} W/K · C {n1(joint.c_kwh_per_k, 1)} ±{" "}
              {n1(joint.c_se, 1)} kWh/K · τ {n1(joint.tau_h, 0)} h · gain {n1(joint.gain_kw, 2)} kW · resid{" "}
              {n1(joint.resid_rms_c, 2)} °C · {joint.n_heat_episodes} heating episodes / {joint.n_coast_blocks} night
              coast blocks
              <span class="muted">
                {" "}
                (pinned UA {d ? n1(d.ua_pinned_w_per_k, 0) : "—"} W/K
                {eff ? `, C ${n1(eff.c_kwh_per_k, 1)} kWh/K, τ ${n1(eff.tau_hours, 0)} h` : ""}; SEs are optimistic —
                quantised counter, COP and lag are model error)
              </span>
            </p>
          ) : (
            <p class="muted">
              Not identifiable yet: {(joint.reason ?? "unknown").replace(/_/g, " ")} ({joint.n_heat_episodes} heating
              episodes / {joint.n_coast_blocks} coast blocks; needs 5 / 8, Onecta-metered heat only)
              {joint.coast_tau_h != null ? ` · coast-only τ ${n1(joint.coast_tau_h, 0)} h` : ""}
            </p>
          )}
          {joint.consistency_flag && (
            <p class="muted">
              Free τ {n1(joint.tau_h, 0)} h disagrees with coast-only τ {n1(joint.coast_tau_h, 0)} h by more than 25 %:
              lag or gain contamination suspected — treat UA / C with caution.
            </p>
          )}
          {joint.tau_fixed && (
            <p class="muted">
              τ fixed ({n1(joint.tau_prior_h, 0)} h, hard constraint): UA {n1(joint.tau_fixed.ua_w_per_k, 0)} W/K · C{" "}
              {n1(joint.tau_fixed.c_kwh_per_k, 1)} kWh/K
            </p>
          )}
          {joint.slot_fit && (
            <p class="muted">
              Per-slot regression (diagnostic): UA {n1(joint.slot_fit.ua_w_per_k, 0)} W/K · C{" "}
              {n1(joint.slot_fit.c_kwh_per_k, 1)} kWh/K · R² {n1(joint.slot_fit.r2, 2)}
            </p>
          )}
          {joint.cop_sensitivity && (
            <p class="muted">
              COP sensitivity:{" "}
              {Object.entries(joint.cop_sensitivity)
                .map(([k, v]) => `${k} → ${v ? `UA ${n1(v.ua_w_per_k, 0)} / C ${n1(v.c_kwh_per_k, 1)}` : "no fit"}`)
                .join(" · ")}
            </p>
          )}
        </div>
      )}
      {bands.length > 0 && (
        <div class="lwtl-scroll">
          <h3 class="lwtl-sub">Cheap-band rise: measured vs predicted ({latest?.date.slice(5)})</h3>
          <table class="lwtl-table">
            <thead>
              <tr>
                <th>Band (local)</th>
                <th class="num">Offset °C</th>
                <th class="num">Measured °C</th>
                <th class="num">Predicted °C</th>
                <th class="num">Model err °C</th>
              </tr>
            </thead>
            <tbody>
              {bands.map((b) => (
                <tr key={b.start_utc} class={b.mixed_plans ? "muted" : undefined} title={b.mixed_plans ? `${b.n_plans} different plans inside this band - not comparable` : undefined}>
                  <td>{localHM(b.start_utc, tz)}–{localHM(b.end_utc, tz)}{b.mixed_plans ? " (mixed plans)" : ""}</td>
                  <td class="num">{n1(b.mean_offset_c, 1)}</td>
                  <td class="num">{n1(b.measured_rise_c, 2)}</td>
                  <td class="num">{n1(b.predicted_rise_c, 2)}</td>
                  <td class="num">{n1(b.model_error_c, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <details class="lwtl-details">
        <summary class="muted">Coast-only UA (circular: C = τ × pinned UA, not a UA measurement)</summary>
        <table class="lwtl-table">
          <thead>
            <tr>
              <th>Day</th>
              <th class="num">UA night W/K</th>
              <th class="num">UA all-coast W/K</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.date}>
                <td>{r.date.slice(5)}</td>
                <td class="num">{n1(r.ua_from_tau_scaled_night_w_per_k ?? r.ua_est_night_w_per_k, 0)}</td>
                <td class="num">{n1(r.ua_from_tau_scaled_w_per_k ?? r.ua_est_w_per_k, 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
      <h3 class="lwtl-sub">Yesterday {d?.yesterday.date.slice(5) ?? ""}: predicted vs realised indoor</h3>
      <Strip slots={d?.yesterday.slots ?? []} />
      <p class="muted lwtl-legend">
        <span class="lwtl-key lwtl-key--pred" /> predicted <span class="lwtl-key lwtl-key--real" /> realised{" "}
        <span class="lwtl-key lwtl-key--floor" /> comfort floor · shaded = coast slot
      </p>
    </section>
  );
}
