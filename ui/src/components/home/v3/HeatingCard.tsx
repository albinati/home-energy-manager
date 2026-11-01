import { useEffect, useState } from "preact/hooks";
import { Icon } from "../../common/Icon";
import { role } from "../../../lib/auth";
import { HemApiError } from "../../../lib/api";
import { toast } from "../../../lib/toast";
import {
  applyBatch, getPlanFronts, getSettings, postComfortFeedback, proposeOptimization, simulateBatch,
  simulateProposeOptimization,
} from "../../../lib/endpoints";
import type { CockpitNow, PlanFrontsResponse, SimulateBatchResponse } from "../../../lib/types";
import { casaTile, currentWindow, fmt, holidayFlags, isAway, num, tierOf } from "../simple/logic.ts";
import {
  CEILING_KEY, SETPOINT_KEY, VERDICTS, comfortWord, errorMessage, feedbackMessage, peakRow, previewText,
  pumpLine, roomChips, runApply, setpointBounds, stepSetpoint, stepperHidden, targetsLine, type Verdict,
} from "./heatingLogic";
import "../../../styles/home-v3-heating.css";

// Home v3 "Aquecimento · Daikin" (#900). Reads only props already polled by
// landing.tsx (/cockpit/now + /plan/fronts). Admin extras: comfort feedback and a
// day-target stepper (simulate -> confirm -> apply with X-Simulation-Id -> replan).

const isSimIdError = (e: unknown): boolean =>
  e instanceof HemApiError && (e.status === 409 || e.status === 410) &&
  /Simulation(Expired|IdRequired|IdMismatch)/.test(e.body || "");

const msg = (e: unknown): string =>
  e instanceof HemApiError ? errorMessage(e.status, e.body) : errorMessage(null, null);

export function HeatingCard({ now, fronts, onChanged }: {
  now: CockpitNow;
  fronts: PlanFrontsResponse | null;
  onChanged: () => Promise<void> | void;
}) {
  const h = fronts?.heating && !fronts.heating.error ? fronts.heating : null;
  const tariff = fronts?.tariff && !fronts.tariff.error ? fronts.tariff : null;
  const nowMs = Date.parse(now.now_utc);
  const t = casaTile(now, fronts);
  const comfort = comfortWord(t.meaning, t.tone);
  const hol = holidayFlags(now);
  const away = isAway(now);
  const controlMode = now.modes?.daikin_control_mode;
  const cur = currentWindow(tariff, nowMs);
  const inPeak = !!cur && tierOf(cur) === "peak";
  const chips = roomChips(now.state.indoor?.rooms ?? null, h?.indoor_rooms_c, h?.outdoor_now_c);
  const pump = pumpLine({
    controlMode, holidayClimate: hol.climate, away, daikinMode: now.state.daikin_mode,
    lwtC: now.state.lwt_c, windows: h?.windows, nowMs, inPeak,
  });
  const peak = hol.climate ? null : peakRow(tariff, h?.windows, h?.predicted_indoor, nowMs);
  const hidden = stepperHidden(controlMode, hol.climate, away);
  const isAdmin = role.value === "admin";
  // The stepper already shows the day target to admins: every number once.
  const stepperShown = isAdmin && !hidden && h?.setpoint_c != null;
  const meta = targetsLine(h?.setpoint_c, h?.night_floor_c, stepperShown);

  return (
    <section class="hs-tile hv3-heat" aria-label="Aquecimento">
      <h2 class="hs-eyebrow"><Icon name="house" size={14} />Aquecimento · Daikin</h2>
      <div class="hs-big">
        <span class="hs-num">{t.value ?? "—"}</span>
        {t.value != null && <span class="hs-unit">°C</span>}
        {comfort.word && <span class={`hv3-word hv3-word--${comfort.tone}`}>{comfort.word}</span>}
      </div>

      {chips.length > 0 && (
        <ul class="hv3-chips" aria-label="Temperatura por cômodo">
          {chips.map((c) => (
            <li class="hv3-chip" key={c.label}><span class="hv3-chip-l">{c.label}</span><strong>{c.value}</strong></li>
          ))}
        </ul>
      )}

      <dl class="hv3-rows">
        <dt>Bomba</dt><dd>{pump}</dd>
        {peak && (<><dt>{peak.label}</dt><dd>{peak.text}</dd></>)}
        {meta && (<><dt>Meta</dt><dd>{meta}</dd></>)}
      </dl>

      {isAdmin && (
        <AdminControls
          setpoint={h?.setpoint_c ?? null} planCeiling={h?.ceiling_c ?? null}
          hidden={hidden} onChanged={onChanged}
        />
      )}
    </section>
  );
}

function AdminControls({ setpoint, planCeiling, hidden, onChanged }: {
  setpoint: number | null;
  planCeiling: number | null;
  hidden: boolean;
  onChanged: () => Promise<void> | void;
}) {
  const [feel, setFeel] = useState<Verdict | null>(null);
  const [fbBusy, setFbBusy] = useState(false);
  const [range, setRange] = useState<{ min?: number; max?: number; ceiling?: number }>({});
  const [draft, setDraft] = useState<number | null>(null);
  const [sim, setSim] = useState<SimulateBatchResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  // Allowed range + live ceiling from the settings metadata (admin-only endpoint;
  // falls back to schema defaults and the plan's ceiling when unreachable).
  useEffect(() => {
    if (hidden) return;
    let alive = true;
    getSettings().then((r) => {
      const s = r.settings.find((x) => x.key === SETPOINT_KEY);
      const c = r.settings.find((x) => x.key === CEILING_KEY);
      if (!alive) return;
      setRange({
        min: s?.min ?? undefined, max: s?.max ?? undefined,
        ceiling: typeof c?.value === "number" ? c.value : undefined,
      });
    }).catch(() => { /* keep defaults */ });
    return () => { alive = false; };
  }, [hidden]);

  const fail = (title: string, e: unknown) => toast.error(title, msg(e));

  const sendFeel = async (v: Verdict) => {
    if (fbBusy) return;
    setFbBusy(true);
    try { await postComfortFeedback(v); setFeel(v); } catch (e) { fail("Não foi possível anotar", e); }
    finally { setFbBusy(false); }
  };

  const bounds = setpointBounds(range.min, range.max, range.ceiling, planCeiling);
  const shown = draft ?? setpoint;
  const dirty = draft != null && setpoint != null && Math.abs(draft - setpoint) > 1e-6;

  const step = (dir: 1 | -1) => {
    if (shown == null || busy) return;
    setSim(null);
    setNotice(null);
    setDraft(stepSetpoint(shown, dir, bounds.min, bounds.max));
  };
  const cancel = () => { setDraft(null); setSim(null); };

  // Explicit "Revisar" (not on every click) so a burst of +/- does not spam simulate.
  const preview = async () => {
    if (!dirty || draft == null) return;
    setBusy(true);
    try { setSim(await simulateBatch({ [SETPOINT_KEY]: draft })); }
    catch (e) { fail("Não foi possível simular", e); }
    finally { setBusy(false); }
  };

  const apply = async () => {
    if (!sim || draft == null || busy) return;
    setBusy(true);
    setNotice(null);
    const want = draft;
    try {
      const out = await runApply({
        apply: async () => {
          try { await applyBatch(sim.simulation_id, { [SETPOINT_KEY]: want }); }
          catch (e) {
            if (isSimIdError(e)) setSim(await simulateBatch({ [SETPOINT_KEY]: want }));
            throw e;
          }
        },
        replan: async () => {
          const diff = await simulateProposeOptimization();
          await proposeOptimization(diff.simulation_id);
        },
        fetchSetpoint: async () => (await getPlanFronts()).heating?.setpoint_c ?? null,
      }, want);
      void onChanged();
      setDraft(null); setSim(null);
      if (out.replanError) {
        setNotice(`Meta salva, mas o plano não foi refeito: ${msg(out.replanError)} Use Replanejar agora.`);
      } else if (out.verified === "no") {
        setNotice("Meta salva e plano refeito, mas a tela ainda mostra a meta antiga. Atualize em instantes.");
      } else {
        toast.success(`Meta de dia: ${num(want)} °C`, "Plano refeito.");
      }
    } catch (e) {
      if (isSimIdError(e)) toast.error("Simulação expirada", "Confira o resumo e confirme de novo.");
      else fail("Não foi possível aplicar", e);
    } finally { setBusy(false); }
  };

  return (
    <div class="hv3-admin">
      <div class="hv3-line">
        <span class="hv3-lab">Como está aí?</span>
        {VERDICTS.map(({ v, label }) => (
          <button key={v} type="button" class={`btn btn--sm${feel === v ? " hv3-on" : ""}`} disabled={fbBusy}
                  aria-pressed={feel === v} onClick={() => void sendFeel(v)}>{label}</button>
        ))}
      </div>
      {feel && <p class="hv3-ok" role="status">{feedbackMessage(feel)}</p>}

      {!hidden && shown != null && (
        <>
          <div class="hv3-line">
            <span class="hv3-lab">Meta de dia</span>
            <button type="button" class="btn btn--sm" aria-label="Diminuir meta" disabled={busy || shown <= bounds.min}
                    onClick={() => step(-1)}>−</button>
            <strong class="hv3-val">{fmt(shown, 1)} °C</strong>
            <button type="button" class="btn btn--sm" aria-label="Aumentar meta" disabled={busy || shown >= bounds.max}
                    onClick={() => step(1)}>+</button>
            {dirty && !sim && (
              <>
                <button type="button" class="btn btn--sm btn--primary" disabled={busy} onClick={() => void preview()}>Revisar</button>
                <button type="button" class="btn btn--sm" disabled={busy} onClick={cancel}>Cancelar</button>
              </>
            )}
          </div>
          {sim && draft != null && setpoint != null && (
            <div class="hv3-confirm">
              <span>{previewText(setpoint, draft)}</span>
              <div class="hv3-line">
                <button type="button" class="btn btn--sm btn--primary" disabled={busy} onClick={() => void apply()}>
                  {busy ? "Aplicando…" : "Aplicar e replanejar"}
                </button>
                <button type="button" class="btn btn--sm" disabled={busy} onClick={cancel}>Cancelar</button>
              </div>
            </div>
          )}
        </>
      )}
      {notice && <p class="hv3-warn" role="status">{notice}</p>}
    </div>
  );
}
