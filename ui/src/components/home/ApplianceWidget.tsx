import type { ApplianceSuggestion, ApplianceJob, Appliance } from "../../lib/types";
import "./appliance.css";

interface ApplianceWidgetProps {
  suggestions?: ApplianceSuggestion[] | null;  // cheapest window per idle appliance
  jobs?: ApplianceJob[] | null;                 // recent jobs (we filter to active)
  appliances?: Appliance[] | null;
}

function fmtLocal(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch {
    return "—";
  }
}

// "Is there an appliance scheduled?" — at a glance, per enabled machine:
//   running → it's washing now
//   scheduled → HEM picked a window (start–end · avg price)
//   idle + cheap window ahead → "load it" prompt (the consent gate is physical)
//   idle, no window → quiet
export function ApplianceWidget({ suggestions, jobs, appliances }: ApplianceWidgetProps) {
  const enabled = (appliances ?? []).filter((a) => a.enabled);

  if (enabled.length === 0) {
    return (
      <div class="appliance appliance--empty">
        <p class="muted">Nenhum eletrodoméstico registrado.</p>
        <p class="appliance-hint">Registre a máquina (SmartThings) para agendar lavagens nas janelas baratas.</p>
      </div>
    );
  }

  const activeJobs = (jobs ?? []).filter((j) => j.status === "scheduled" || j.status === "running");
  const jobByApp = new Map(activeJobs.map((j) => [j.appliance_id, j]));
  const sugByApp = new Map((suggestions ?? []).map((s) => [s.appliance_id, s]));

  return (
    <div class="appliance">
      {enabled.map((a) => {
        const job = jobByApp.get(a.id);
        const sug = sugByApp.get(a.id);
        return (
          <div class="appliance-row" key={a.id}>
            <div class="appliance-name">🧺 {a.name}</div>

            {job ? (
              job.status === "running" ? (
                <div class="appliance-state appliance-state--run">Rodando agora</div>
              ) : (
                <div class="appliance-state appliance-state--sched">
                  Programado <strong>{fmtLocal(job.planned_start_utc)}–{fmtLocal(job.planned_end_utc)}</strong>
                  {job.avg_price_pence != null && (
                    <span class="appliance-price"> · {job.avg_price_pence.toFixed(1)}p/kWh</span>
                  )}
                </div>
              )
            ) : sug ? (
              <div class="appliance-state appliance-state--idle">
                {sug.meets_threshold === false ? (
                  // No cheap window ahead → still show the NEXT/cheapest available.
                  <>
                    <span class="appliance-tag appliance-tag--next">próxima</span>
                    <strong>{fmtLocal(sug.recommended_start_utc)}–{fmtLocal(sug.recommended_end_utc)}</strong>
                    <span class="appliance-price"> · {sug.avg_price_pence.toFixed(1)}p (sem janela barata)</span>
                    <div class="appliance-cta">Carregue + Smart Control até {sug.deadline_local}</div>
                  </>
                ) : (
                  <>
                    <span class={sug.is_negative ? "appliance-tag appliance-tag--paid" : "appliance-tag appliance-tag--cheap"}>
                      {sug.is_negative ? "janela paga" : "janela barata"}
                    </span>
                    <strong>{fmtLocal(sug.recommended_start_utc)}–{fmtLocal(sug.recommended_end_utc)}</strong>
                    <span class="appliance-price"> · {sug.avg_price_pence.toFixed(1)}p</span>
                    <div class="appliance-cta">Carregue + Smart Control até {sug.deadline_local}</div>
                  </>
                )}
              </div>
            ) : (
              <div class="appliance-state appliance-state--none muted">Sem janela disponível antes do prazo</div>
            )}
          </div>
        );
      })}
    </div>
  );
}
