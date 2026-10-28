import { Link } from "wouter-preact";
import type { PlanFrontsResponse } from "../../../lib/types";
import {
  dayAxis, frac, tariffSegments, gridLabel, heatingCell, waterSegments, hourWord, minutesOf, tierOf, TIER_WORD,
} from "./logic";

// The day as four lanes of CSS segments (no chart library): Tarifa / Rede /
// Aquecimento / Água quente, with a "now" marker. On a banded tariff the axis
// is the real bands from /plan/fronts.tariff; a dynamic tariff arrives as
// tier runs already and is drawn the same way.
export function BandStrip({ fronts, nowMs, holiday }: { fronts: PlanFrontsResponse; nowMs: number; holiday: boolean }) {
  const tariff = fronts.tariff && !fronts.tariff.error ? fronts.tariff : null;
  const axis = dayAxis(tariff);
  if (!tariff || !axis) return <p class="hs-muted">Sem tarifas para hoje.</p>;

  const pct = (f: number) => `${(f * 100).toFixed(3)}%`;
  const pos = (a: number, b: number) => ({ left: `calc(${pct(a)} + 1px)`, width: `calc(${pct(b - a)} - 2px)` });

  const wins = [...tariff.windows].sort((a, b) => Date.parse(a.start_utc) - Date.parse(b.start_utc)).map((w) => ({
    w, a: frac(axis, Date.parse(w.start_utc)), b: frac(axis, Date.parse(w.end_utc)),
  }));
  const segs = tariffSegments(tariff, axis);
  const water = waterSegments(fronts.tank, axis);
  const nowF = frac(axis, nowMs);
  const showNow = nowMs >= axis.startMs && nowMs <= axis.endMs;

  // Hour ruler: band boundaries on a banded tariff, 6-hourly otherwise.
  const ticks: Array<{ f: number; label: string }> = [];
  if (tariff.structure === "banded" && wins.length <= 9) {
    const seen = new Set<string>();
    for (const { w, a } of wins) {
      if (seen.has(w.start_local)) continue;
      seen.add(w.start_local);
      ticks.push({ f: a, label: hourWord(w.start_local).slice(0, 2) });
    }
  } else {
    const base = minutesOf(wins[0].w.start_local);
    for (const h of [0, 6, 12, 18]) {
      const f = ((h * 60 - base + 1440) % 1440) / 1440;
      ticks.push({ f, label: String(h).padStart(2, "0") });
    }
  }

  return (
    <div class="hs-strip">
      <div class="hs-lanes">
        <div class="hs-lane">
          <span class="hs-lane-label">Tarifa</span>
          <div class="hs-track">
            {segs.map((s, i) => (
              <span key={i} class={`hs-seg hs-seg--${s.tier}`} style={pos(s.start, s.end)}
                    title={`${TIER_WORD[s.tier]} ${s.startLocal}–${s.endLocal}${s.label ? ` · ${s.label}` : ""}`}>
                {s.label}
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
                      style={pos(a, b)} title={text}>{text}</span>
              );
            })}
          </div>
        </div>

        <div class="hs-lane">
          <span class="hs-lane-label">Aquecimento</span>
          <div class="hs-track">
            {wins.map(({ w, a, b }, i) => {
              const cell = heatingCell(w, fronts.heating, holiday);
              if (!cell) return null;
              return <span key={i} class={`hs-seg hs-seg--heat-${cell.mode}`} style={pos(a, b)} title={cell.text}>{cell.text}</span>;
            })}
          </div>
        </div>

        <div class="hs-lane">
          <span class="hs-lane-label">Água quente</span>
          <div class="hs-track">
            {water.map((s, i) => (
              <span key={i} class={`hs-seg ${s.warm && !holiday ? "hs-seg--water" : "hs-seg--water-idle"}`}
                    style={pos(s.start, s.end)} title={s.text}>{holiday ? "desligado" : s.text}</span>
            ))}
          </div>
        </div>

        <div class="hs-lane hs-lane--ruler">
          <span class="hs-lane-label" />
          <div class="hs-track hs-ruler">
            {ticks.map((t, i) => <span key={i} class="hs-tick" style={{ left: pct(t.f) }}>{t.label}</span>)}
          </div>
        </div>

        {showNow && <span class="hs-now" style={{ "--f": nowF } as never} aria-label="agora" />}
      </div>
      <p class="hs-caption">
        Azul = compra da rede · amarelo = bomba de calor aquecendo · roxo = tanque quente. Linha branca = agora.{" "}
        <Link href="/insights" class="hs-link">Detalhes e histórico →</Link>
      </p>
    </div>
  );
}
