import { useState } from "preact/hooks";
import type { SchedulerTimeline, DispatchDecisionsResponse, TimelineSlot } from "../../lib/types";
import { hhmm, pence, slotKindLabel, slotKindColorVar } from "../../lib/format";

interface DispatchPlanStripProps {
  timeline: SchedulerTimeline | null;
  decisions: DispatchDecisionsResponse | null;
}

// Renders the LP plan's per-slot dispatch as a horizontal strip with a SoC
// trajectory line drawn above. Hovering a cell shows the LP reasoning so
// the operator can answer "why is this slot peak_export?" without crossing
// to another page.
export function DispatchPlanStrip({ timeline, decisions }: DispatchPlanStripProps) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  if (!timeline) {
    return <div class="dispatch-strip-empty muted">No plan yet.</div>;
  }

  // Combine executed + ongoing + planned into one chronological list.
  const slots: TimelineSlot[] = [
    ...(timeline.executed || []),
    ...(timeline.ongoing ? [timeline.ongoing] : []),
    ...(timeline.planned || []),
  ];

  if (slots.length === 0) {
    return <div class="dispatch-strip-empty muted">No slots in the active plan.</div>;
  }

  const nowIdx = (timeline.executed?.length || 0);
  const decisionByTime = new Map<string, string>();
  for (const d of decisions?.decisions || []) {
    if (d.reason) decisionByTime.set(d.slot_time_utc, d.reason);
  }

  // Compute SoC polyline; min/max for y-scale.
  const socPoints: Array<{ idx: number; soc: number }> = [];
  let minSoc = 100;
  let maxSoc = 0;
  slots.forEach((s, i) => {
    if (s.soc_percent != null && Number.isFinite(s.soc_percent)) {
      socPoints.push({ idx: i, soc: s.soc_percent });
      if (s.soc_percent < minSoc) minSoc = s.soc_percent;
      if (s.soc_percent > maxSoc) maxSoc = s.soc_percent;
    }
  });
  const socRange = Math.max(20, maxSoc - minSoc + 10);
  const socBase = Math.max(0, minSoc - 5);

  const hoverSlot = hoverIdx != null ? slots[hoverIdx] : null;
  const hoverKind = hoverSlot ? (hoverSlot.dispatched_kind || hoverSlot.lp_kind || "standard") : "";
  const hoverReason = hoverSlot ? decisionByTime.get(hoverSlot.slot_time_utc) ?? hoverSlot.reason ?? null : null;

  // SoC trajectory — same math, rendered as an area-filled ribbon. Smoothing
  // only changes the render, never the plotted soc_percent values.
  const stripW = 1000; // viewBox units
  const socH = 40;
  const pts = socPoints.map(({ idx, soc }) => ({
    x: (idx / slots.length) * stripW,
    y: socH - ((soc - socBase) / socRange) * socH,
  }));
  const socPolyline = pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  const socArea = pts.length
    ? `${pts[0].x.toFixed(1)},${socH} ${socPolyline} ${pts[pts.length - 1].x.toFixed(1)},${socH}`
    : "";

  // Live playhead position (fraction of the strip width).
  const playheadPct = slots.length > 0 ? (nowIdx / slots.length) * 100 : 0;

  return (
    <div class="dispatch-strip">
      {/* SoC trajectory — area ribbon riding above the cells */}
      <div class="dispatch-strip-soc" aria-label="Predicted state of charge">
        <svg viewBox={`0 0 ${stripW} ${socH}`} preserveAspectRatio="none">
          <defs>
            <linearGradient id="soc-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.22" />
              <stop offset="100%" stop-color="var(--accent)" stop-opacity="0" />
            </linearGradient>
          </defs>
          {socArea && <polygon points={socArea} fill="url(#soc-fill)" stroke="none" />}
          <polyline
            points={socPolyline}
            fill="none"
            stroke="var(--accent)"
            stroke-width="2"
            vector-effect="non-scaling-stroke"
          />
        </svg>
        <span class="dispatch-strip-soc-axis dispatch-strip-soc-axis--top">{Math.round(socBase + socRange)}%</span>
        <span class="dispatch-strip-soc-axis dispatch-strip-soc-axis--bot">{Math.round(socBase)}%</span>
      </div>

      {/* Dispatch cells + live playhead */}
      <div class="dispatch-strip-cells" role="presentation">
        {slots.map((s, i) => {
          const kind = s.dispatched_kind || s.lp_kind || "standard";
          const isPast = i < nowIdx;
          return (
            <div
              key={s.slot_time_utc}
              class={`dispatch-cell${isPast ? " is-past" : ""}${hoverIdx === i ? " is-hover" : ""}`}
              style={{ background: slotKindColorVar(kind) }}
              title={`${hhmm(s.slot_time_utc)} · ${slotKindLabel(kind)}`}
              onMouseEnter={() => setHoverIdx(i)}
              onMouseLeave={() => setHoverIdx((prev) => (prev === i ? null : prev))}
            />
          );
        })}
        <span class="soc-playhead" style={{ left: `${playheadPct}%` }} aria-hidden="true" />
      </div>

      <div class="dispatch-strip-axis">
        {[0, 6, 12, 18, 24, 30, 36, 42, 48].map((h) => (
          <span key={h}>{h}h</span>
        ))}
      </div>

      {/* Detail panel under the strip */}
      <div class="dispatch-strip-detail">
        {hoverSlot ? (
          <>
            <div class="dispatch-strip-detail-head">
              <span class="dispatch-strip-detail-time">{hhmm(hoverSlot.slot_time_utc)}</span>
              <span class="dispatch-strip-detail-kind" style={{ color: slotKindColorVar(hoverKind) }}>
                {slotKindLabel(hoverKind)}
              </span>
              {hoverSlot.fox_mode && <span class="dispatch-strip-detail-mode">{hoverSlot.fox_mode}</span>}
              {hoverSlot.price_import_p != null && (
                <span class="dispatch-strip-detail-price">
                  import {pence(hoverSlot.price_import_p)}
                  {hoverSlot.price_export_p != null && <> · export {pence(hoverSlot.price_export_p)}</>}
                </span>
              )}
              {hoverSlot.soc_percent != null && (
                <span class="dispatch-strip-detail-soc">SoC {hoverSlot.soc_percent.toFixed(0)}%</span>
              )}
            </div>
            {hoverReason && <div class="dispatch-strip-detail-reason">{hoverReason}</div>}
          </>
        ) : (
          <div class="dispatch-strip-detail-hint">
            Hover any slot to see the LP's reasoning. Blue line above = predicted state of charge.
          </div>
        )}
      </div>

      <div class="dispatch-strip-legend">
        <Swatch kind="negative" label="Negative price" />
        <Swatch kind="cheap" label="Cheap charge" />
        <Swatch kind="solar_charge" label="Solar charge" />
        <Swatch kind="standard" label="Standard" />
        <Swatch kind="peak" label="Peak avoid" />
        <Swatch kind="peak_export" label="Peak export" />
      </div>
    </div>
  );
}

function Swatch({ kind, label }: { kind: string; label: string }) {
  return (
    <span class="dispatch-swatch">
      <span class="dispatch-swatch-dot" style={{ background: slotKindColorVar(kind) }} />
      {label}
    </span>
  );
}
