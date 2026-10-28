import { useState } from "preact/hooks";
import { useFetch } from "../lib/poll";
import { getActionLog } from "../lib/endpoints";
import { Spinner } from "../components/common/Spinner";
import { Pill } from "../components/common/Pill";
import { SensorJournal } from "../components/report/SensorJournal";
import { groupByDay, hhmm } from "../lib/format";
import "./report.css";

// Registro de ações (journal) — what the system ACTUALLY did (tank / battery / appliances),
// read straight from action_log. This is the audit trail: every fired, skipped
// or failed action with its trigger, tariff context and result.
// A second view (Sensors) shows the lossless room-sensor ingest audit
// (device_reading_log, #540 W1c) — everything each device POSTed.

type JournalView = "actions" | "sensors";
const VIEWS: { key: JournalView; label: string; blurb: string }[] = [
  { key: "actions", label: "Ações",
    blurb: "Tudo o que o sistema realmente fez — tanque, bateria e eletrodomésticos — com o gatilho e o resultado." },
  { key: "sensors", label: "Sensores",
    blurb: "Tudo o que os sensores dos cômodos enviaram — cada leitura por dispositivo, exatamente como chegou." },
];

type DeviceFilter = "all" | "daikin" | "foxess" | "appliance";
const DEVICE_TABS: { key: DeviceFilter; label: string }[] = [
  { key: "all", label: "Tudo" },
  { key: "daikin", label: "Tanque / bomba de calor" },
  { key: "foxess", label: "Bateria" },
  { key: "appliance", label: "Eletrodomésticos" },
];
const RANGES = [
  { days: 1, label: "24h" },
  { days: 7, label: "7 dias" },
  { days: 30, label: "30 dias" },
];

function deviceLabel(d: string): string {
  switch (d) {
    case "daikin": return "Tanque";
    case "foxess": case "fox": return "Bateria";
    case "appliance": return "Eletrodoméstico";
    default: return d;
  }
}
function deviceTone(d: string): "thermal" | "power" | "appliance" | "neutral" {
  switch (d) {
    case "daikin": return "thermal";
    case "foxess": case "fox": return "power";
    case "appliance": return "appliance";
    default: return "neutral";
  }
}

// Humanise the raw action verbs into something readable.
function actionLabel(a: string): string {
  const map: Record<string, string> = {
    tank_warmup: "Tanque: aquecimento",
    tank_setback: "Tanque: reduzir",
    tank_negative_boost: "Tanque: reforço (preço negativo)",
    max_heat: "Tanque: aquecimento máximo",
    pre_heat: "Tanque: pré-aquecimento",
    shutdown: "Tanque: desligar",
    restore: "Voltar ao normal",
    charge: "Bateria: carga forçada",
    discharge: "Bateria: descarga forçada",
    mode_switch: "Bateria: troca de modo",
    washer_start: "Máquina de lavar: início",
    dryer_start: "Secadora: início",
    dishwasher_start: "Lava-louças: início",
    apply_safe_defaults: "Aplicar padrões seguros",
  };
  return map[a] || a.replace(/_/g, " ");
}
function resultTone(r: string): "ok" | "bad" | "dim" {
  if (r === "success") return "ok";
  if (r === "failed") return "bad";
  return "dim";
}

function kindLabel(k: string): string {
  const m: Record<string, string> = {
    cheap: "faixa barata", standard: "faixa dia", peak: "pico", negative: "preço negativo",
    solar_charge: "carga solar", solar_preheat: "pré-aquecimento solar", idle: "parado",
  };
  return m[k] || k.replace(/_/g, " ");
}

// Pick the most informative param to show inline (temp, offset, power, soc).
function paramHint(p: Record<string, unknown>): string | null {
  const bits: string[] = [];
  if (typeof p.tank_temp === "number") bits.push(`${p.tank_temp}°C`);
  if (typeof p.lwt_offset === "number" && p.lwt_offset !== 0) bits.push(`LWT ${p.lwt_offset > 0 ? "+" : ""}${p.lwt_offset}`);
  if (typeof p.fdSoc === "number") bits.push(`→ ${p.fdSoc}%`);
  if (typeof p.fdPwr === "number") bits.push(`${p.fdPwr}W`);
  if (p.tank_powerful === true) bits.push("potente");
  return bits.length ? bits.join(" · ") : null;
}

export default function Report() {
  const [view, setView] = useState<JournalView>("actions");
  // Filter state for BOTH views lives here so switching tabs (which unmounts
  // the inactive view) doesn't reset the filters mid-audit.
  const [actionDevice, setActionDevice] = useState<DeviceFilter>("all");
  const [actionDays, setActionDays] = useState(7);
  const [sensorDevice, setSensorDevice] = useState<string | null>(null);
  const [sensorHours, setSensorHours] = useState(24);
  const blurb = VIEWS.find((v) => v.key === view)!.blurb;

  return (
    <div class="page-padded report">
      <header class="report-head">
        <div class="report-title-row">
          <h1>Registro de ações</h1>
          <div class="report-tabs" role="tablist" aria-label="Visão do registro">
            {VIEWS.map((v) => (
              <button key={v.key} role="tab" aria-selected={view === v.key}
                      class={`report-tab${view === v.key ? " active" : ""}`}
                      onClick={() => setView(v.key)}>{v.label}</button>
            ))}
          </div>
        </div>
        <p class="muted">{blurb}</p>
      </header>

      {view === "actions" ? (
        <ActionsJournal device={actionDevice} setDevice={setActionDevice}
                        days={actionDays} setDays={setActionDays} />
      ) : (
        <SensorJournal device={sensorDevice} setDevice={setSensorDevice}
                       hours={sensorHours} setHours={setSensorHours} />
      )}
    </div>
  );
}

interface ActionsJournalProps {
  device: DeviceFilter;
  setDevice: (d: DeviceFilter) => void;
  days: number;
  setDays: (d: number) => void;
}

function ActionsJournal({ device, setDevice, days, setDays }: ActionsJournalProps) {
  const log = useFetch(
    () => getActionLog({ device: device === "all" ? undefined : device, days, limit: 300 }),
    [device, days],
  );

  const entries = log.data?.entries ?? [];
  // Group by local day, preserving the DESC order the API returns.
  const groups = groupByDay(entries, (e) => e.timestamp);

  return (
    <>
      <div class="report-filters">
        <div class="report-tabs" role="tablist" aria-label="Dispositivo">
          {DEVICE_TABS.map((t) => (
            <button key={t.key} role="tab" aria-selected={device === t.key}
                    class={`report-tab${device === t.key ? " active" : ""}`}
                    onClick={() => setDevice(t.key)}>{t.label}</button>
          ))}
        </div>
        <div class="report-range">
          {RANGES.map((r) => (
            <button key={r.days} class={`report-tab${days === r.days ? " active" : ""}`}
                    onClick={() => setDays(r.days)}>{r.label}</button>
          ))}
        </div>
      </div>

      {log.loading && !log.data && <Spinner label="Carregando registro…" />}
      {log.error && <p class="report-error">Não foi possível carregar o registro: {log.error.message}</p>}
      {!log.loading && entries.length === 0 && (
        <p class="muted report-empty">Nenhuma ação registrada neste período.</p>
      )}

      {groups.map((g) => (
        <section key={g.day} class="report-group">
          <h2 class="report-group-day">{g.day}</h2>
          <ul class="report-list">
            {g.items.map((e) => {
              const hint = paramHint(e.params || {});
              return (
                <li key={e.id} class="report-row">
                  <span class="report-time">{hhmm(e.timestamp)}</span>
                  <span class={`report-dot report-dot--${deviceTone(e.device)}`} />
                  <span class="report-device">{deviceLabel(e.device)}</span>
                  <span class="report-action">
                    {actionLabel(e.action)}
                    {hint && <span class="report-hint"> · {hint}</span>}
                  </span>
                  <span class="report-meta">
                    {e.slot_kind && <span class="report-kind">{kindLabel(e.slot_kind)}</span>}
                    {e.agile_price_at_time != null && (
                      <span class="report-price">{e.agile_price_at_time.toFixed(1)}p</span>
                    )}
                    {e.trigger && <span class="report-trigger" title="O que agendou esta ação">{e.trigger}</span>}
                    <Pill tone={resultTone(e.result)}>{e.result}</Pill>
                  </span>
                  {e.error_msg && e.result !== "success" && (
                    <span class="report-err" title={e.error_msg}>{e.error_msg}</span>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </>
  );
}
