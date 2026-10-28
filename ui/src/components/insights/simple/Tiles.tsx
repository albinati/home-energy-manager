import { useState } from "preact/hooks";
import { lazy, Suspense } from "preact/compat";
import type { ComponentChildren } from "preact";
import type { RecHouse, RecWeekSummary } from "../../../lib/recTypes";
import { Spinner } from "../../common/Spinner";
import type { DetailKey } from "./DetailCards";
import {
  bandSegments, batteryCaption, billCaption, energyCaption, fmt, houseTiles, pct, sgbp, solarCaption,
} from "./logic";

// Lazy: ECharts and every detailed card live in this chunk, fetched on first expand.
const DetailCards = lazy(() => import("./DetailCards"));

/** "ver mais" under a tile: expands the EXISTING detailed card for that topic. */
function MoreBox({ which, label }: { which: DetailKey; label: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div class="is-more">
      <button type="button" class="is-more-btn" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "ver menos" : "ver mais"}
      </button>
      {open && (
        <div class="is-more-body" aria-label={label}>
          <Suspense fallback={<Spinner label="Carregando…" />}>
            <DetailCards which={which} />
          </Suspense>
        </div>
      )}
    </div>
  );
}

function Tile({ title, value, unit, children, more, moreLabel }: {
  title: string; value: string; unit?: string; children?: ComponentChildren;
  more?: DetailKey; moreLabel?: string;
}) {
  return (
    <section class="is-tile" aria-label={title}>
      <h3 class="is-eyebrow">{title}</h3>
      <div class="is-big"><span class="is-num">{value}</span>{unit && <span class="is-unit">{unit}</span>}</div>
      {children}
      {more && <MoreBox which={more} label={moreLabel ?? title} />}
    </section>
  );
}

export function WeekTiles({ ws }: { ws: RecWeekSummary }) {
  const e = ws.energy;
  const segs = e ? bandSegments(e) : [];
  const s = ws.solar;
  const b = ws.battery;
  return (
    <div class="is-tiles">
      <Tile title="Conta" value={ws.net_gbp != null ? sgbp(ws.net_gbp) : "—"} more="bill" moreLabel="Comparação de tarifas">
        <p class="is-cap">{billCaption(ws) || "Sem dados medidos nesta semana."}</p>
      </Tile>
      <Tile title="Energia" value={e?.total_kwh != null ? fmt(e.total_kwh, 0) : "—"} unit="kWh" more="energy"
            moreLabel="Consumo por hora">
        {segs.length > 0 && (
          <>
            <div class="is-bar" role="img"
                 aria-label={segs.map((x) => `${x.label} ${Math.round(x.pct)} %`).join(", ")}>
              {segs.map((x) => <span key={x.key} class={`is-seg is-seg--${x.key}`} style={{ width: `${x.pct}%` }} />)}
            </div>
            <ul class="is-legend">
              {segs.map((x) => (
                <li key={x.key}><i class={`is-dot is-seg--${x.key}`} />{x.label} {pct(e!.shares[x.key])}</li>
              ))}
            </ul>
          </>
        )}
        {e && <p class="is-cap">{energyCaption(e)}</p>}
      </Tile>
      <Tile title="Sol" value={s?.kwh != null ? fmt(s.kwh, 0) : "—"} unit="kWh" more="solar" moreLabel="Geração">
        <p class="is-cap">{s ? solarCaption(s) : "Sem geração medida nesta semana."}</p>
      </Tile>
      <Tile title="Bateria" value={b?.arbitrage_gbp != null ? sgbp(b.arbitrage_gbp) : "—"} unit="arbitragem"
            more="battery" moreLabel="Placar diário">
        <p class="is-cap">{b ? batteryCaption(b) : "Sem uso da bateria nesta semana."}</p>
      </Tile>
    </div>
  );
}

export function HouseTiles({ house }: { house: RecHouse }) {
  const tiles = houseTiles(house);
  if (tiles.length === 0) {
    return <p class="is-muted">Ainda aprendendo a casa — os números aparecem quando há dados suficientes.</p>;
  }
  const more: Record<string, DetailKey | undefined> = { cooling: "cooling", heating: "heating" };
  return (
    <div class="is-tiles">
      {tiles.map((t) => (
        <Tile key={t.key} title={t.title} value={t.value} unit={t.unit} more={more[t.key]}>
          <p class="is-cap">{t.caption}</p>
        </Tile>
      ))}
    </div>
  );
}

/** Earlier tuning reviews, behind a link under "O que fazer". */
export function EarlierReviews() {
  return <MoreBox which="tuning" label="Revisões anteriores" />;
}
