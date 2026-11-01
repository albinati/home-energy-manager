import type { CockpitNow } from "../../../lib/types";
import { Icon } from "../../common/Icon";
import { agoraStale } from "../simple/logic";
import { flowModel, flowCaption, fmt, type FlowPill } from "./dayChart";
import "../../../styles/home-v3-day.css";

// "Agora" (#898): live house balance as flow pills. Left = where the energy comes
// from (sol, rede importando, bateria descarregando), right = where it goes (casa,
// bateria carregando, rede exportando). Source: /cockpit/now state (10 s poll), already
// loaded by the page. Stale Fox data keeps the "(há N min)" marker.
function Pill({ p }: { p: FlowPill }) {
  return (
    <div class="dc-pill">
      <span class={`dc-dot dc-dot--${p.key}`} aria-hidden="true" />
      <span class="dc-pill-label">{p.label}</span>
      <strong class="dc-pill-kw">{fmt(p.kw, 1)} kW</strong>
    </div>
  );
}

export function AgoraFlow({ now }: { now: CockpitNow }) {
  const m = flowModel(now.state);
  if (m.sources.length === 0 && m.sinks.length === 0) return null;
  const stale = agoraStale(now);
  return (
    <section class="hs-card dc-flow" aria-label="Agora">
      <h2 class="hs-eyebrow">Agora · energia indo e vindo{stale}</h2>
      <div class="dc-flow-row">
        <div class="dc-flow-group" role="group" aria-label="Entra">
          {m.sources.map((p) => <Pill key={p.key + p.label} p={p} />)}
        </div>
        <span class="dc-flow-arrow" aria-hidden="true"><Icon name="chevron" size={20} /></span>
        <div class="dc-flow-group" role="group" aria-label="Vai para">
          {m.sinks.map((p) => <Pill key={p.key + p.label} p={p} />)}
        </div>
      </div>
      <p class="dc-flow-cap">{flowCaption(m)}</p>
    </section>
  );
}
