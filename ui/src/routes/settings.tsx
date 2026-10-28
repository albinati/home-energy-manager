import { useMemo, useState } from "preact/hooks";
import { useFetch } from "../lib/poll";
import {
  getSettings,
  simulateBatch,
  applyBatch,
} from "../lib/endpoints";
import { toast } from "../lib/toast";
import { HemApiError } from "../lib/api";
import { Spinner } from "../components/common/Spinner";
import { Icon } from "../components/common/Icon";
import { Modal } from "../components/common/Modal";
import { Link } from "wouter-preact";
import { SettingField } from "../components/settings/SettingField";
import { BatchBar } from "../components/settings/BatchBar";
import {
  VISIBLE_GROUPS, ADVANCED_GROUPS, labelFor, filterAdvanced, modeLine, safetyFlagText, isGenericSummary,
  parseApiError,
} from "../components/settings/groups";
import { toDatetimeLocal, isAwayPreset } from "../components/home/simple/logic";
import { plannerTz } from "../lib/plannerTz";
import type { SettingSpec, SimulateBatchResponse } from "../lib/types";
import "../components/settings/settings.css";
import "../components/settings/settings-advanced.css";

function isEqual(a: unknown, b: unknown): boolean {
  if (typeof a === "number" && typeof b === "number") return Math.abs(a - b) < 1e-9;
  return a === b;
}

export default function Settings() {
  const settings = useFetch(getSettings, []);
  const [pending, setPending] = useState<Record<string, unknown>>({});
  const [busy, setBusy] = useState(false);
  const [simResult, setSimResult] = useState<SimulateBatchResponse | null>(null);
  const [simOpen, setSimOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [advOpen, setAdvOpen] = useState(false);
  const [applyErrors, setApplyErrors] = useState<Record<string, string>>({});

  const specByKey = useMemo(() => {
    const map = new Map<string, SettingSpec>();
    (settings.data?.settings || []).forEach((s) => map.set(s.key, s));
    return map;
  }, [settings.data]);

  // Same reading as the Home ModeBox: aliases count as away, the stored return
  // time is local wall-clock (the server lower-cases it, "t12:00:00z").
  const presetVal = String(specByKey.get("OPTIMIZATION_PRESET")?.value ?? "");
  const modeText = modeLine(
    isAwayPreset(presetVal) ? "vacation" : presetVal,
    toDatetimeLocal(specByKey.get("VACATION_RETURN_AT")?.value),
    plannerTz.value,
  );

  const onChange = (key: string, value: unknown) => {
    const spec = specByKey.get(key);
    if (!spec) return;
    setPending((prev) => {
      if (isEqual(value, spec.value)) {
        const next = { ...prev };
        delete next[key];
        return next;
      }
      return { ...prev, [key]: value };
    });
  };

  const onRevert = (key: string) => {
    setPending((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
  };

  const onDiscardAll = () => setPending({});

  const pendingCount = useMemo(() => {
    let n = 0;
    for (const k of Object.keys(pending)) {
      const spec = specByKey.get(k);
      if (spec && !isEqual(pending[k], spec.value)) n += 1;
    }
    return n;
  }, [pending, specByKey]);

  const pendingIn = (keys: string[]) => {
    let n = 0;
    for (const k of keys) {
      const spec = specByKey.get(k);
      if (spec && k in pending && !isEqual(pending[k], spec.value)) n += 1;
    }
    return n;
  };

  const onSimulate = async () => {
    if (pendingCount === 0) return;
    setBusy(true);
    try {
      const cleanChanges: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(pending)) {
        const spec = specByKey.get(k);
        if (spec && !isEqual(v, spec.value)) cleanChanges[k] = v;
      }
      const result = await simulateBatch(cleanChanges);
      setSimResult(result);
      setSimOpen(true);
    } catch (e) {
      const err = e as HemApiError;
      toast.error("Falha na simulação", parseApiError(err.body, err.message).message);
    } finally {
      setBusy(false);
    }
  };

  const onApply = async () => {
    if (!simResult) return;
    setBusy(true);
    setApplyErrors({});
    try {
      // Re-derive {KEY: proposed} from the simulated sub_actions (per-key
      // after is a single-entry {KEY: value} object).
      const changes = Object.fromEntries(simResult.sub_actions.map((sa) => [sa.key, sa.after[sa.key]]));
      const result = await applyBatch(simResult.simulation_id, changes);
      toast.success(`${result.results.length} ajuste${result.results.length === 1 ? "" : "s"} aplicado${result.results.length === 1 ? "" : "s"}`);
      setPending({});
      setSimOpen(false);
      setSimResult(null);
      await settings.refresh();
    } catch (e) {
      const err = e as HemApiError;
      if (err instanceof HemApiError && (err.status === 409 || err.status === 410) &&
          /Simulation(Expired|IdRequired|IdMismatch)/.test(err.body || "")) {
        // Sat past the 5 min TTL (or the id was used): re-simulate, show the fresh diff, ask again.
        try {
          const cleanChanges = Object.fromEntries(simResult.sub_actions.map((sa) => [sa.key, sa.after[sa.key]]));
          setSimResult(await simulateBatch(cleanChanges));
          toast.error("Simulação expirada", "Confira o resumo atualizado e confirme de novo.");
        } catch (e2) {
          const err2 = e2 as HemApiError;
          toast.error("Falha na simulação", parseApiError(err2.body, err2.message).message);
        }
      } else {
        // 409 BatchPartialFailure: per-key failures go under their fields; earlier keys were rolled back.
        const info = parseApiError(err.body, err.message);
        setApplyErrors(info.byKey);
        if (Object.keys(info.byKey).length > 0) setSimOpen(false);
        setSimResult(null);
        toast.error("Falha ao aplicar", info.message);
      }
    } finally {
      setBusy(false);
    }
  };

  if (settings.loading) {
    return (
      <div class="settings-page">
        <Spinner label="Carregando ajustes…" />
      </div>
    );
  }
  if (settings.error) {
    return (
      <div class="settings-page">
        <h1>Ajustes</h1>
        <p class="muted">Falha ao carregar: {settings.error.message}</p>
        <button class="btn" onClick={() => settings.refresh()}>Tentar de novo</button>
      </div>
    );
  }

  const advancedAll = ADVANCED_GROUPS.flatMap((g) => g.keys);
  const advancedPending = pendingIn(advancedAll);
  const searching = query.trim().length > 0;
  const advancedShown = filterAdvanced(ADVANCED_GROUPS, specByKey, query);

  const renderFields = (keys: string[], plain: boolean) =>
    keys
      .map((k) => specByKey.get(k))
      .filter((sp): sp is SettingSpec => !!sp)
      .map((spec) => (
        <SettingField
          key={spec.key}
          spec={spec}
          plain={plain}
          pending={pending[spec.key]}
          error={applyErrors[spec.key]}
          onChange={onChange}
          onRevert={onRevert}
        />
      ));

  return (
    <div class="settings-page">
      <header class="settings-header">
        <div>
          <h1 class="settings-header-title">Ajustes</h1>
          <p class="settings-header-sub">
            Edite os valores; <strong>Simular</strong> mostra o que muda; <strong>Aplicar</strong> grava.
          </p>
        </div>
      </header>

      <section class="settings-mode" aria-label="Modo da casa">
        <span class="settings-mode-label">Modo da casa</span>
        <strong class="settings-mode-value">{modeText}</strong>
        <Link href="/" class="settings-mode-link">mudar na Casa</Link>
      </section>

      {VISIBLE_GROUPS.map((g) => (
        <div class="settings-section" key={g.id}>
          <div class="settings-section-header">
            <div class="settings-section-title">
              {g.title}
              {pendingIn(g.keys) > 0 && <span class="settings-tab-badge">{pendingIn(g.keys)}</span>}
            </div>
            {g.subtitle && <div class="settings-section-subtitle">{g.subtitle}</div>}
          </div>
          <div class="setting-group-fields">{renderFields(g.keys, true)}</div>
        </div>
      ))}

      <details class="settings-advanced" open={advOpen || searching} onToggle={(e) => setAdvOpen((e.currentTarget as HTMLDetailsElement).open)}>
        <summary>
          <span class="settings-advanced-title">Avançado</span>
          <span class="settings-advanced-sub">todos os outros ajustes ({advancedAll.length})</span>
          {advancedPending > 0 && <span class="settings-tab-badge">{advancedPending}</span>}
        </summary>
        <div class="settings-advanced-body">
          <input
            type="search"
            class="settings-search"
            placeholder="Buscar por nome, chave ou descrição"
            aria-label="Buscar ajustes avançados"
            value={query}
            onInput={(e) => setQuery((e.currentTarget as HTMLInputElement).value)}
          />
          {advancedShown.length === 0 && <p class="muted">Nenhum ajuste encontrado.</p>}
          {advancedShown.map((g) => (
            <details class="settings-adv-group" key={g.id} open={searching ? true : undefined}>
              <summary>
                <span class="settings-section-title">{g.title}</span>
                <span class="settings-adv-count">{g.keys.length}</span>
                {pendingIn(g.keys) > 0 && <span class="settings-tab-badge">{pendingIn(g.keys)}</span>}
              </summary>
              {g.subtitle && <div class="settings-section-subtitle settings-adv-sub">{g.subtitle}</div>}
              <div class="setting-group-fields">{renderFields(g.keys, false)}</div>
            </details>
          ))}
        </div>
      </details>

      <BatchBar
        pendingCount={pendingCount}
        busy={busy}
        stage={simOpen ? (simResult ? "apply" : "simulate") : "edit"}
        onSimulate={onSimulate}
        onDiscardAll={onDiscardAll}
      />

      <Modal
        open={simOpen && !!simResult}
        onClose={() => {
          if (!busy) {
            setSimOpen(false);
            setSimResult(null);
          }
        }}
        title="Revisar alterações"
        width="lg"
        footer={
          <>
            <button class="btn btn--ghost" onClick={() => setSimOpen(false)} disabled={busy}>
              Cancelar
            </button>
            <button class="btn btn--primary" onClick={onApply} disabled={busy}>
              {busy ? "Aplicando…" : `Aplicar ${simResult?.sub_actions.length || 0} alteraç${(simResult?.sub_actions.length || 0) === 1 ? "ão" : "ões"}`}
            </button>
          </>
        }
      >
        {simResult && (
          <>
            <table class="sim-diff">
              <thead>
                <tr>
                  <th>Ajuste</th>
                  <th>Mudança</th>
                </tr>
              </thead>
              <tbody>
                {simResult.sub_actions.map((d) => (
                  <tr key={d.key}>
                    <td>
                      <div>{labelFor(d.key)}</div>
                      <div class="muted"><code>{d.key}</code></div>
                      {!isGenericSummary(d.human_summary, d.key) && <div class="muted">{d.human_summary}</div>}
                    </td>
                    <td>
                      <code class="from">{String(d.before[d.key])}</code>
                      <span class="arrow"><Icon name="chevron" size={12} /></span>
                      <code class="to">{String(d.after[d.key])}</code>
                      {specByKey.get(d.key)?.cron_reload && (
                        <div class="muted">Vale no próximo reinício</div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {simResult.safety_flags.length > 0 && (
              <div class="sim-warnings">
                <strong>Avisos:</strong>
                <ul>
                  {simResult.safety_flags.map((w, i) => <li key={i}>{safetyFlagText(w)}</li>)}
                </ul>
              </div>
            )}
          </>
        )}
      </Modal>
    </div>
  );
}
