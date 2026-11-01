import { Fragment } from "preact";
import { Icon } from "../../common/Icon";
import type { CockpitNow, PlanFrontsResponse, StatusFoxDriftBlock } from "../../../lib/types";
import { agoraStale } from "../simple/logic";
import { batteryCardModel } from "./batteryCard";
import "../../../styles/home-v3-battery.css";

// Read-only on purpose: the plan decides when to charge (no manual control).
export function BatteryCard({ now, fronts, drift }: {
  now: CockpitNow; fronts: PlanFrontsResponse | null; drift?: Pick<StatusFoxDriftBlock, "in_sync"> | null;
}) {
  const m = batteryCardModel(now, fronts, drift);
  const pct = m.socPct != null ? Math.max(0, Math.min(100, m.socPct)) : 0;
  return (
    <section class="hv3-batt" aria-label="Bateria">
      <h2 class="hs-eyebrow"><Icon name="battery" size={14} />Bateria</h2>
      <div class="hs-big">
        <span class="hs-num">{m.socPct != null ? Math.round(m.socPct) : "—"}</span>
        {m.socPct != null && <span class="hs-unit">%</span>}
        {m.stateWord && <span class={`hv3-batt-state hv3-batt-state--${m.stateTone}`}>{m.stateWord}{agoraStale(now)}</span>}
      </div>
      {m.socPct != null && (
        <div>
          <div class="hv3-batt-bar" role="img" aria-label={`Carga ${Math.round(pct)}%`}>
            <div class="hv3-batt-fill" style={{ width: `${pct}%` }} />
            {m.reservePct != null && <div class="hv3-batt-reserve" title="reserva" style={{ left: `${m.reservePct}%` }} />}
          </div>
          <div class="hv3-batt-scale">
            <span>{m.reservePct != null ? `reserva ${Math.round(m.reservePct)}%` : ""}</span>
            <span>{m.target ? `meta ${m.target.pct}% às ${m.target.at}` : ""}</span>
          </div>
        </div>
      )}
      {m.rows.length > 0 && (
        <dl class="hv3-batt-rows">
          {m.rows.map((r) => (
            <Fragment key={r.key}>
              <dt>{r.label}</dt>
              <dd class={r.tone === "dim" ? "" : `hv3-tone-${r.tone}`}>{r.text}</dd>
            </Fragment>
          ))}
        </dl>
      )}
      <p class="hv3-batt-note">Quem decide quando carregar é o plano; sem botão manual de propósito.</p>
    </section>
  );
}
