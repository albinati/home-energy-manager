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
import { Modal } from "../components/common/Modal";
import { ModeSwitcher } from "../components/settings/ModeSwitcher";
import { SettingField } from "../components/settings/SettingField";
import { BatchBar } from "../components/settings/BatchBar";
import { SettingsTabs } from "../components/settings/SettingsTabs";
import { SETTINGS_GROUPS, labelFor } from "../components/settings/groups";
import type { SettingSpec, SimulateBatchResponse } from "../lib/types";
import "../components/settings/settings.css";

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
  const [activeTab, setActiveTab] = useState<string>(SETTINGS_GROUPS[0].id);

  const specByKey = useMemo(() => {
    const map = new Map<string, SettingSpec>();
    (settings.data?.settings || []).forEach((s) => map.set(s.key, s));
    return map;
  }, [settings.data]);

  const modeSpec = specByKey.get("OPTIMIZATION_PRESET") || null;

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

  // Pending count per group, for the tab badges.
  const pendingByGroup = useMemo(() => {
    const out: Record<string, number> = {};
    for (const g of SETTINGS_GROUPS) {
      let n = 0;
      for (const k of g.keys) {
        const spec = specByKey.get(k);
        if (spec && k in pending && !isEqual(pending[k], spec.value)) n += 1;
      }
      if (n > 0) out[g.id] = n;
    }
    return out;
  }, [pending, specByKey]);

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
      toast.error("Simulation failed", err.body || err.message);
    } finally {
      setBusy(false);
    }
  };

  const onApply = async () => {
    if (!simResult) return;
    setBusy(true);
    try {
      const changes = simResult.diffs.map((d) => ({ key: d.key, value: d.proposed }));
      const result = await applyBatch(simResult.simulation_id, changes);
      if (result.errors && result.errors.length > 0) {
        toast.error(
          `${result.errors.length} key${result.errors.length === 1 ? "" : "s"} failed`,
          result.errors.map((e) => `${e.key}: ${e.error}`).join("\n"),
        );
      } else {
        toast.success(`Applied ${result.applied.length} setting${result.applied.length === 1 ? "" : "s"}`);
        setPending({});
      }
      setSimOpen(false);
      setSimResult(null);
      await settings.refresh();
    } catch (e) {
      const err = e as HemApiError;
      toast.error("Apply failed", err.body || err.message);
    } finally {
      setBusy(false);
    }
  };

  if (settings.loading) {
    return (
      <div class="settings-page">
        <Spinner label="Loading settings…" />
      </div>
    );
  }
  if (settings.error) {
    return (
      <div class="settings-page">
        <h1>Settings</h1>
        <p class="muted">Failed to load settings: {settings.error.message}</p>
        <button class="btn" onClick={() => settings.refresh()}>Retry</button>
      </div>
    );
  }

  const activeGroup = SETTINGS_GROUPS.find((g) => g.id === activeTab) || SETTINGS_GROUPS[0];
  const activeSpecs = activeGroup.keys
    .map((k) => specByKey.get(k))
    .filter((s): s is SettingSpec => !!s);

  return (
    <div class="settings-page">
      <header class="settings-header">
        <div>
          <div class="settings-header-eyebrow">Runtime configuration</div>
          <h1 class="settings-header-title">Settings</h1>
          <p class="settings-header-sub">
            Three-step flow: <strong>Edit</strong> values inline (yellow "Edited" pill marks pending), then <strong>Simulate</strong> opens a diff modal, then <strong>Apply</strong> writes to <code>runtime_settings</code>. Schedule keys hot-reload the cron jobs.
          </p>
        </div>
      </header>

      {modeSpec && (
        <ModeSwitcher
          spec={modeSpec}
          pendingValue={pending.OPTIMIZATION_PRESET as string | undefined}
          onChange={(v) => onChange("OPTIMIZATION_PRESET", v)}
          onRevert={() => onRevert("OPTIMIZATION_PRESET")}
        />
      )}

      <SettingsTabs
        groups={SETTINGS_GROUPS}
        activeId={activeTab}
        pendingByGroup={pendingByGroup}
        onSelect={setActiveTab}
      />

      <div class="settings-section">
        <div class="settings-section-header">
          <div class="settings-section-title">{activeGroup.title}</div>
          <div class="settings-section-subtitle">{activeGroup.subtitle}</div>
        </div>
        <div class="setting-group-fields">
          {activeSpecs.map((spec) => (
            <SettingField
              key={spec.key}
              spec={spec}
              pending={pending[spec.key]}
              onChange={onChange}
              onRevert={onRevert}
            />
          ))}
        </div>
      </div>

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
        title="Review changes"
        width="lg"
        footer={
          <>
            <button class="btn btn--ghost" onClick={() => setSimOpen(false)} disabled={busy}>
              Cancel
            </button>
            <button class="btn btn--primary" onClick={onApply} disabled={busy}>
              {busy ? "Applying…" : `Apply ${simResult?.diffs.length || 0} change${(simResult?.diffs.length || 0) === 1 ? "" : "s"}`}
            </button>
          </>
        }
      >
        {simResult && (
          <>
            <table class="sim-diff">
              <thead>
                <tr>
                  <th>Setting</th>
                  <th>Change</th>
                </tr>
              </thead>
              <tbody>
                {simResult.diffs.map((d) => (
                  <tr key={d.key}>
                    <td>
                      <div>{labelFor(d.key)}</div>
                      <div class="muted"><code>{d.key}</code></div>
                    </td>
                    <td>
                      <code class="from">{String(d.current)}</code>
                      <span class="arrow">→</span>
                      <code class="to">{String(d.proposed)}</code>
                      {d.cron_reload && (
                        <div class="muted">Hot-reloads scheduler</div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {simResult.warnings && simResult.warnings.length > 0 && (
              <div class="sim-warnings">
                <strong>Warnings:</strong>
                <ul>
                  {simResult.warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              </div>
            )}
          </>
        )}
      </Modal>
    </div>
  );
}
