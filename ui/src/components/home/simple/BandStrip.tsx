import { useEffect, useRef, useState } from "preact/hooks";
import { Link } from "wouter-preact";
import type { PlanFrontsResponse } from "../../../lib/types";
import {
  dayAxis, frac, tariffSegments, gridLabel, heatingCell, waterSegments, hourWord, hourTicks, bandRows,
  tierOf, TIER_WORD, pp, span, type HolidayFlags,
} from "./logic";

// A segment shows its text only when it is at least this wide; the full text is
// always in the tooltip and in the phone list.
const MIN_TEXT_PX = 56;

// The day as four lanes of CSS segments (no chart library): Tarifa / Rede /
// Aquecimento / Água quente, with a "now" marker. At <= 720 px the same data is
// a vertical list, one row per tariff band, current band highlighted.
export function BandStrip({ fronts, nowMs, holiday, tz }: {
  fronts: PlanFrontsResponse; nowMs: number; holiday: HolidayFlags; tz?: string;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [trackPx, setTrackPx] = useState(0);
  useEffect(() => {
    const el = trackRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setTrackPx(el.clientWidth));
    ro.observe(el);
    setTrackPx(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  const tariff = fronts.tariff && !fronts.tariff.error ? fronts.tariff : null;
  const axis = dayAxis(tariff);
  if (!tariff || !axis) return <p class="hs-muted">Sem tarifas para hoje.</p>;

  const pct = (f: number) => `${(f * 100).toFixed(3)}%`;
  const pos = (a: number, b: number) => ({ left: `calc(${pct(a)} + 1px)`, width: `calc(${pct(b - a)} - 2px)` });
  const fits = (a: number, b: number) => (b - a) * trackPx >= MIN_TEXT_PX;

  const wins = [...tariff.windows].sort((a, b) => Date.parse(a.start_utc) - Date.parse(b.start_utc)).map((w) => ({
    w, a: frac(axis, Date.parse(w.start_utc)), b: frac(axis, Date.parse(w.end_utc)),
  }));
  const segs = tariffSegments(tariff, axis);
  const water = waterSegments(fronts.tank, axis);
  const rows = bandRows(fronts, nowMs, holiday);
  const nowF = frac(axis, nowMs);
  const showNow = nowMs >= axis.startMs && nowMs <= axis.endMs;

  // Hour ruler: band boundaries on a banded tariff, 6-hourly (real axis) otherwise.
  const ticks: Array<{ f: number; label: string }> = [];
  if (tariff.structure === "banded" && wins.length <= 9) {
    const seen = new Set<string>();
    for (const { w, a } of wins) {
      if (seen.has(w.start_local)) continue;
      seen.add(w.start_local);
      ticks.push({ f: a, label: hourWord(w.start_local).slice(0, 2) });
    }
  } else {
    for (const t of hourTicks(axis, tz, [0, 6, 12, 18])) ticks.push({ f: t.f, label: String(t.hour).padStart(2, "0") });
  }

  return (
    <div class="hs-strip">
      <div class="hs-lanes">
        <div class="hs-lanes-main">
          <div class="hs-lane">
            <span class="hs-lane-label">Tarifa</span>
            <div class="hs-track" ref={trackRef}>
              {segs.map((s, i) => (
                <span key={i} class={`hs-seg hs-seg--${s.tier}`} style={pos(s.start, s.end)}
                      title={`${TIER_WORD[s.tier]} ${s.startLocal}–${s.endLocal}${s.label ? ` · ${s.label}` : ""}`}>
                  {fits(s.start, s.end) ? s.label : ""}
                </span>
              ))}
            </div>
          </div>

          <div class="hs-lane">
            <span class="hs-lane-label">Rede</span>
            <div class="hs-track">
              {wins.map(({ w, a, b }, i) => {
                const text = gridLabel(w, fronts.battery);
                if (!text) return null;
                const peak = tierOf(w) === "peak";
                return (
                  <span key={i} class={`hs-seg hs-seg--grid${peak ? " hs-seg--goal" : ""}${w.status === "upcoming" ? " is-plan" : ""}`}
                        style={pos(a, b)} title={text}>{fits(a, b) ? text : ""}</span>
                );
              })}
            </div>
          </div>

          <div class="hs-lane">
            <span class="hs-lane-label">Aquecimento</span>
            <div class="hs-track">
              {wins.map(({ w, a, b }, i) => {
                const cell = heatingCell(w, fronts.heating, holiday.climate);
                if (!cell) return null;
                return <span key={i} class={`hs-seg hs-seg--heat-${cell.mode}`} style={pos(a, b)} title={cell.text}>{fits(a, b) ? cell.text : ""}</span>;
              })}
            </div>
          </div>

          <div class="hs-lane">
            <span class="hs-lane-label">Água quente</span>
            <div class="hs-track">
              {water.map((s, i) => (
                <span key={i} class={`hs-seg ${s.warm && !holiday.tank ? "hs-seg--water" : "hs-seg--water-idle"}`}
                      style={pos(s.start, s.end)} title={holiday.tank ? "desligado (holiday)" : s.text}>
                  {fits(s.start, s.end) ? (holiday.tank ? "desligado" : s.text) : ""}
                </span>
              ))}
            </div>
          </div>

          {showNow && <span class="hs-now" style={{ "--f": nowF } as never} aria-hidden="true" />}
        </div>

        <div class="hs-lane hs-lane--ruler">
          <span class="hs-lane-label" />
          <div class="hs-track hs-ruler">
            {ticks.map((t, i) => <span key={i} class="hs-tick" style={{ left: pct(t.f) }}>{t.label}</span>)}
          </div>
        </div>
      </div>

      <ul class="hs-bandlist" aria-label="Faixas do dia">
        {rows.map((r, i) => (
          <li key={i} class={`hs-bandrow${r.current ? " is-now" : ""}`} aria-current={r.current ? "true" : undefined}>
            <span class="hs-bandrow-head">
              <span class={`hs-chip hs-chip--${r.tier}`}>{span(r.startLocal, r.endLocal)}</span>
              <span class="hs-bandrow-tier">{TIER_WORD[r.tier]}{r.price != null ? ` ${pp(r.price)}` : ""}</span>
              {r.current && <span class="hs-bandrow-now">agora</span>}
            </span>
            <span class="hs-bandrow-body">
              {r.grid && <span>rede {r.grid}</span>}
              {r.heat && <span>{r.heat.text}</span>}
              {r.water && <span>água {r.water}</span>}
            </span>
          </li>
        ))}
      </ul>

      <p class="hs-caption">
        Azul = compra da rede · laranja = aquecendo (bomba de calor e tanque). Linha vertical = agora.{" "}
        <Link href="/insights" class="hs-link">Detalhes e histórico →</Link>
      </p>
    </div>
  );
}
