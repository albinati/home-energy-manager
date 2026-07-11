import type { ComponentChildren } from "preact";
import { usePoll } from "../../lib/poll";
import { getStatusFeedback } from "../../lib/endpoints";
import { Icon } from "../common/Icon";
import { Widget } from "../common/Widget";
import { TriggersFeed } from "./TriggersFeed";
import { role } from "../../lib/auth";
import type { PvTodayResponse } from "../../lib/types";
import "./feedback.css";

// "Self-check" — answers "are the recent changes actually working?" without
// a trip to the Journal or the DB: forecast provenance (Quartz sidecar vs
// fallback), today's PV forecast accuracy, the DHW budget vs what the tank
// really used (auto-scale, #534), and the LWT pre-heat demand gate (#540).
// Degrades silently when /status/feedback doesn't exist yet (older image).
//
// PV accuracy comes in as a prop: landing already polls /pv/today for the
// hero — no second fetch for the same payload.

const fmtKwh = (v: number | null | undefined, dp = 1) =>
  v == null ? "—" : `${v.toFixed(dp)} kWh`;

function ageLabel(ageS: number | null | undefined): string {
  if (ageS == null) return "?";
  if (ageS < 90) return "now";
  if (ageS < 5400) return `${Math.round(ageS / 60)}m ago`;
  return `${Math.round(ageS / 3600)}h ago`;
}

export function FeedbackPanel({ pv }: { pv: PvTodayResponse | null }) {
  const fb = usePoll(getStatusFeedback, 5 * 60_000);
  const d = fb.data;

  // Older hem image (404), transient error, or still loading → render nothing
  // (the panel owns its whole widget band, so no empty card is left behind).
  if (!d) return null;

  const f = d.forecast;
  const forecastOk = !f.degraded;
  const forecastName = f.model_name === "quartz-open-site" ? "Quartz (self-hosted)" : (f.model_name ?? "unknown");

  const dhw = d.dhw;
  const budget = dhw.effective_budget_kwh;
  const measured = dhw.measured_today_kwh;
  const dhwPct = budget && budget > 0 && measured != null
    ? Math.min(100, (measured / budget) * 100)
    : null;
  const dhwTip = [
    `nominal ${fmtKwh(dhw.nominal_kwh)} × auto-scale ${dhw.autoscale_factor != null ? dhw.autoscale_factor.toFixed(2) : "—"}${dhw.autoscale_enabled ? "" : " (disabled)"}`,
    `7-day measured avg ${fmtKwh(dhw.measured_7d_avg_kwh)}`,
    `mode: ${dhw.mode}`,
  ].join(" · ");

  const gate = d.lwt_gate;
  let gateLabel: string;
  let gateOk = true;
  if (!gate.preheat_enabled) {
    gateLabel = "Pre-heat off";
  } else if (!gate.gate_enabled) {
    gateLabel = "Pre-heat ungated";
    gateOk = false; // gate disabled = offsets can fire with zero heating demand
  } else if (gate.preheat_suppressed) {
    gateLabel = "Pre-heat gated — no heating demand";
  } else {
    gateLabel = `Pre-heat active — ${fmtKwh(gate.measured_window_kwh)} heating in ${gate.lookback_hours}h`;
  }
  const gateTip = `measured space heating ${fmtKwh(gate.measured_window_kwh, 2)} over ${gate.lookback_hours}h vs ${fmtKwh(gate.threshold_kwh, 2)} threshold (HEM's own offset windows excluded)`;

  const acc = pv?.accuracy ?? null;

  // Observational: the price-aware warmup resolver is OFF by default; this row
  // shows what it WOULD pick vs the static hour so the delta can be watched
  // toward a winter enable-decision. Tone is neutral (ok=null) — no verdict.
  const warmup = d.dhw_warmup_shadow;
  const hh = (h: number) => `${String(h).padStart(2, "0")}:00`;
  const warmupValue = warmup
    ? warmup.static_hour === warmup.would_pick_hour
      ? `${hh(warmup.static_hour)} · no change`
      : `${hh(warmup.static_hour)} → would pick ${hh(warmup.would_pick_hour)}${
          warmup.delta_pence != null
            ? ` · Δ ${warmup.delta_pence >= 0 ? "+" : ""}${warmup.delta_pence.toFixed(1)}p/slot`
            : ""
        }`
    : "not yet resolved today";
  const warmupTip = warmup
    ? `shadow of the price-aware warmup resolver (${warmup.enabled ? "ENABLED" : "off — observational"}); resolved ${ageLabel(
        (Date.now() - Date.parse(warmup.resolved_at)) / 1000,
      )}`
    : "price-aware warmup would-pick lands once the Agile window is fully published";

  return (
    <div class="widget-grid widget-band">
      <Widget title="Self-check" icon={<Icon name="check" size={14} />} tone="plan" size="wide">
        <div class="selfcheck">
          <div class="selfcheck-row">
            <SelfCheckItem
              ok={forecastOk}
              label="PV forecast"
              value={`${forecastName} · ${ageLabel(f.age_s)}`}
              tip={`source=${f.source ?? "?"} · sidecar ${f.sidecar_ok == null ? "n/a" : f.sidecar_ok ? "healthy" : "DOWN"}`}
            />
            <SelfCheckItem
              ok={acc ? acc.mae_kwh <= 0.5 : null}
              label="PV accuracy today"
              value={acc ? `MAE ${acc.mae_kwh.toFixed(2)} kWh · bias ${acc.bias_kwh >= 0 ? "+" : ""}${acc.bias_kwh.toFixed(2)}` : "no slots yet"}
              tip={acc ? `${acc.slots_compared} half-hours compared · forecast ${acc.forecast_kwh.toFixed(1)} vs actual ${acc.actual_kwh.toFixed(1)} kWh` : undefined}
            />
            <SelfCheckItem
              ok={dhwPct == null ? null : dhwPct <= 100}
              label="Hot water budget"
              value={`${fmtKwh(measured)} of ${fmtKwh(budget)}`}
              tip={dhwTip}
            >
              {dhwPct != null && (
                <span class="selfcheck-bar" aria-hidden="true">
                  <span class="selfcheck-bar-fill" style={{ width: `${dhwPct}%` }} />
                </span>
              )}
            </SelfCheckItem>
            <SelfCheckItem ok={gateOk} label="Heating pre-heat" value={gateLabel} tip={gateTip} />
            <SelfCheckItem ok={null} label="DHW warmup (shadow)" value={warmupValue} tip={warmupTip} />
          </div>
          {role.value === "admin" && <TriggersFeed />}
        </div>
      </Widget>
    </div>
  );
}

function SelfCheckItem({
  ok,
  label,
  value,
  tip,
  children,
}: {
  ok: boolean | null; // null = informational, no verdict
  label: string;
  value: string;
  tip?: string;
  children?: ComponentChildren;
}) {
  return (
    <div class="selfcheck-item" title={tip}>
      <span class="selfcheck-label">
        {ok != null && (
          <span class={`selfcheck-dot ${ok ? "is-ok" : "is-warn"}`} aria-hidden="true">
            <Icon name={ok ? "check" : "warn"} size={11} />
          </span>
        )}
        {label}
      </span>
      <span class="selfcheck-value">{value}</span>
      {children}
    </div>
  );
}
