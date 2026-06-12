import { useState } from "preact/hooks";
import { useFetch } from "../../lib/poll";
import {
  getSettings,
  getSchedulerStatus,
  simulateBatch,
  applyBatch,
  simulateProposeOptimization,
  proposeOptimization,
  simulateSchedulerPause,
  pauseScheduler,
  simulateSchedulerResume,
  resumeScheduler,
  cancelApplianceJob,
} from "../../lib/endpoints";
import { HemApiError } from "../../lib/api";
import { MODE_META } from "../settings/ModeSwitcher";
import { Modal } from "../common/Modal";
import { Icon } from "../common/Icon";
import { toast } from "../../lib/toast";
import { role } from "../../lib/auth";
import type {
  ActionDiffResponse,
  SimulateBatchResponse,
  Appliance,
  ApplianceJob,
} from "../../lib/types";
import "./operate.css";

// "Operate" — the admin control cluster at the top of the live band. Four
// actions, all behind the same lockbar consent as HeatingControls:
//   1. household mode (normal/guests/vacation) via /settings/batch — the SAME
//      path the Settings page uses (NEVER /optimization/preset: legacy enum
//      that bypasses runtime_settings),
//   2. replan now (/optimization/propose, simulate→confirm),
//   3. scheduler pause/resume,
//   4. cancel a scheduled appliance run.
// Every write rides the X-Simulation-Id flow (REQUIRE_SIMULATION_ID is on in
// prod): simulate → diff modal → confirm. An expired sim-id (409/410) re-
// simulates once and asks again on the refreshed diff.

interface OperateCardProps {
  appliances?: Appliance[];
  applianceJobs?: ApplianceJob[];
  // Called after any applied action so the route can refresh its polls
  // (timeline, heating plan, appliance jobs).
  onChanged: () => void;
}

type Confirm =
  | { kind: "mode"; mode: string; sim: SimulateBatchResponse }
  | { kind: "replan" | "pause" | "resume"; diff: ActionDiffResponse };

const MODE_KEY = "OPTIMIZATION_PRESET";

export function OperateCard({ appliances, applianceJobs, onChanged }: OperateCardProps) {
  // Hooks run unconditionally (stable hook order); the admin gate is BELOW
  // them. The route also gates the mount on role — that's what keeps a
  // viewer from ever firing the admin-only GET /settings below.
  const settings = useFetch(getSettings, []);
  // Fetch-once, NOT a poll: GET /scheduler/status fires a live Octopus rates
  // fetch server-side on every call (review M on #555) — once per admin visit
  // is plenty, and pause/resume below refresh it explicitly.
  const sched = useFetch(getSchedulerStatus, []);

  const [unlocked, setUnlocked] = useState(false);
  const [confirmingUnlock, setConfirmingUnlock] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [replanSuggested, setReplanSuggested] = useState(false);

  const modeSpec = settings.data?.settings.find((s) => s.key === MODE_KEY);
  const currentMode = modeSpec ? String(modeSpec.value) : null;
  const modes = (modeSpec?.enum ?? ["normal", "guests", "vacation"]) as string[];

  // Admin-only surface (after hooks — stable order if the role flips live).
  if (role.value !== "admin") return null;

  const paused = sched.data?.paused === true;
  const scheduled = (applianceJobs ?? []).filter((j) => j.status === "scheduled");
  const applianceName = (id: number) =>
    appliances?.find((a) => a.id === id)?.name ?? `appliance #${id}`;

  const guard = <T,>(fn: () => Promise<T>): Promise<T | null> => {
    if (busy) return Promise.resolve(null);
    setBusy(true);
    return fn()
      .catch((e) => {
        // HemApiError.message is just "hem-api 409 Conflict" — the actionable
        // detail (e.g. "job N is in status running") lives in .body.
        const detail = e instanceof HemApiError ? (e.body || e.message)
          : e instanceof Error ? e.message : String(e);
        toast.error("Operate action failed", detail);
        return null;
      })
      .finally(() => setBusy(false));
  };

  // Discriminate sim-id lifecycle errors from other 409s (e.g. the batch
  // apply's 409 BatchPartialFailure) — status alone is ambiguous.
  const isSimIdError = (e: unknown): e is HemApiError =>
    e instanceof HemApiError &&
    (e.status === 409 || e.status === 410) &&
    /Simulation(Expired|IdRequired|IdMismatch)/.test(e.body || "");

  // ── mode (settings batch — same flow as the Settings page) ──────────────
  const chooseMode = (m: string) => {
    if (!unlocked || m === currentMode) return;
    void guard(async () => {
      const sim = await simulateBatch({ [MODE_KEY]: m });
      setConfirm({ kind: "mode", mode: m, sim });
    });
  };

  const confirmMode = (c: Extract<Confirm, { kind: "mode" }>) =>
    void guard(async () => {
      try {
        await applyBatch(c.sim.simulation_id, { [MODE_KEY]: c.mode });
      } catch (e) {
        // Modal sat open past the 5-min sim TTL → re-simulate once and ask
        // again on the fresh diff. Other failures (409 BatchPartialFailure
        // etc.) fall through to guard()'s toast.
        if (isSimIdError(e)) {
          setConfirm({ kind: "mode", mode: c.mode, sim: await simulateBatch({ [MODE_KEY]: c.mode }) });
          toast.error("Simulation expired", "Review the refreshed diff and confirm again.");
          return;
        }
        throw e;
      }
      toast.success(`Household mode → ${c.mode}`);
      setReplanSuggested(true);
      void settings.refresh();
      onChanged();
      setConfirm(null);
    });

  // ── ActionDiff-based actions (replan / pause / resume) ──────────────────
  const startDiffAction = (kind: "replan" | "pause" | "resume") => {
    if (!unlocked) return;
    const sim =
      kind === "replan" ? simulateProposeOptimization
      : kind === "pause" ? simulateSchedulerPause
      : simulateSchedulerResume;
    void guard(async () => {
      setConfirm({ kind, diff: await sim() });
    });
  };

  const confirmDiffAction = (c: Extract<Confirm, { kind: "replan" | "pause" | "resume" }>) =>
    void guard(async () => {
      const act = () =>
        c.kind === "replan" ? proposeOptimization(c.diff.simulation_id)
        : c.kind === "pause" ? pauseScheduler(c.diff.simulation_id)
        : resumeScheduler(c.diff.simulation_id);
      try {
        await act();
      } catch (e) {
        // Expired/consumed sim-id → re-simulate ONCE and re-ask on the fresh
        // diff (the state may have changed since the stale one was rendered).
        if (isSimIdError(e)) {
          const sim =
            c.kind === "replan" ? simulateProposeOptimization
            : c.kind === "pause" ? simulateSchedulerPause
            : simulateSchedulerResume;
          setConfirm({ kind: c.kind, diff: await sim() });
          toast.error("Simulation expired", "Review the refreshed diff and confirm again.");
          return;
        }
        throw e;
      }
      toast.success(
        c.kind === "replan" ? "Replan started — new plan applies automatically"
        : c.kind === "pause" ? "Scheduler paused"
        : "Scheduler resumed",
      );
      setConfirm(null);
      if (c.kind === "replan") setReplanSuggested(false);
      else void sched.refresh();
      onChanged();
    });

  // ── appliance job cancel (no simulate pair — see endpoints.ts) ──────────
  const cancelJob = (job: ApplianceJob) =>
    void guard(async () => {
      await cancelApplianceJob(job.id);
      toast.success(`Cancelled ${applianceName(job.appliance_id)} run`);
      onChanged();
    });

  return (
    <section class="operate" aria-label="Operate">
      <header class="operate-head">
        <span class="operate-title"><Icon name="settings" size={13} /> Operate</span>
        {paused && <span class="operate-paused-pill">scheduler paused</span>}
        <span class="grow" />
        <button
          type="button"
          class={`btn btn--sm${unlocked ? " btn--ghost" : " btn--primary"}`}
          aria-pressed={unlocked}
          onClick={() => (unlocked ? setUnlocked(false) : setConfirmingUnlock(true))}
        >
          <Icon name={unlocked ? "unlock" : "lock"} size={12} />
          {unlocked ? "Re-lock" : "Take control"}
        </button>
      </header>

      <div class={`operate-body${unlocked ? "" : " is-locked"}`}>
        {/* mode segment */}
        <div class="operate-group">
          <span class="operate-label">Household mode</span>
          <div class="operate-modes" role="radiogroup" aria-label="Household mode">
            {modes.map((m) => {
              const meta = MODE_META[m] ?? { label: m, sub: "", icon: "settings" as const };
              const active = m === currentMode;
              return (
                <button
                  key={m}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  class={`operate-mode${active ? " is-active" : ""}`}
                  disabled={!unlocked || busy || !modeSpec}
                  title={meta.sub}
                  onClick={() => chooseMode(m)}
                >
                  <Icon name={meta.icon} size={13} />
                  {meta.label}
                </button>
              );
            })}
          </div>
        </div>

        {/* plan + scheduler actions */}
        <div class="operate-group operate-actions">
          <span class="operate-label">Plan</span>
          <div class="operate-btnrow">
            <button
              type="button"
              class={`btn btn--sm${replanSuggested ? " btn--primary" : ""}`}
              disabled={!unlocked || busy}
              title="Re-run the LP optimizer now (simulates first; the new plan uploads to Fox + writes the Daikin schedule)"
              onClick={() => startDiffAction("replan")}
            >
              <Icon name="revert" size={12} /> Replan now
            </button>
            <button
              type="button"
              class={`btn btn--sm${paused ? " operate-resume" : ""}`}
              disabled={!unlocked || busy || sched.data == null}
              title={paused ? "Resume automatic dispatch" : "Pause the scheduler (no automatic Daikin/Fox writes until resumed)"}
              onClick={() => startDiffAction(paused ? "resume" : "pause")}
            >
              <Icon name={paused ? "power-live" : "schedule"} size={12} />
              {paused ? "Resume scheduler" : "Pause scheduler"}
            </button>
          </div>
          {replanSuggested && (
            <span class="operate-hint">Mode changed — replan to apply it to today's dispatch.</span>
          )}
        </div>

        {/* scheduled appliance runs */}
        {scheduled.length > 0 && (
          <div class="operate-group">
            <span class="operate-label">Scheduled appliances</span>
            <ul class="operate-jobs">
              {scheduled.map((j) => (
                <li key={j.id} class="operate-job">
                  <span class="operate-job-what">
                    {applianceName(j.appliance_id)}
                    {j.planned_start_utc && (
                      <span class="operate-job-when">
                        {" "}· {new Date(j.planned_start_utc).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}
                      </span>
                    )}
                    {j.avg_price_pence != null && (
                      <span class="operate-job-when"> · {j.avg_price_pence.toFixed(1)}p avg</span>
                    )}
                  </span>
                  <button
                    type="button"
                    class="btn btn--sm btn--ghost"
                    disabled={!unlocked || busy}
                    onClick={() => cancelJob(j)}
                  >
                    Cancel
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* unlock consent */}
      <Modal
        open={confirmingUnlock}
        onClose={() => setConfirmingUnlock(false)}
        width="sm"
        title="Enable operate controls?"
        footer={
          <>
            <button class="btn btn--ghost" onClick={() => setConfirmingUnlock(false)}>Cancel</button>
            <button class="btn btn--primary" onClick={() => { setUnlocked(true); setConfirmingUnlock(false); }}>
              Enable
            </button>
          </>
        }
      >
        <p>Unlock mode, replan and scheduler controls. Each action still shows
           a simulated diff you confirm before anything is written.</p>
        <p class="muted">The system otherwise runs itself — re-lock when you're done.</p>
      </Modal>

      {/* confirm: settings-batch diff (mode) */}
      <Modal
        open={confirm?.kind === "mode"}
        onClose={() => setConfirm(null)}
        width="sm"
        title="Change household mode?"
        footer={
          confirm?.kind === "mode" && (
            <>
              <button class="btn btn--ghost" disabled={busy} onClick={() => setConfirm(null)}>Cancel</button>
              <button class="btn btn--primary" disabled={busy} onClick={() => confirmMode(confirm)}>
                {busy ? "Applying…" : `Apply ${confirm.mode}`}
              </button>
            </>
          )
        }
      >
        {confirm?.kind === "mode" && (
          <>
            <ul class="operate-diff">
              {confirm.sim.sub_actions.map((d) => (
                <li key={d.key}>
                  <code>{d.key}</code>: <strong>{String(d.before[d.key])}</strong> → <strong>{String(d.after[d.key])}</strong>
                </li>
              ))}
            </ul>
            {confirm.sim.safety_flags.length > 0 && (
              <p class="operate-warnings"><Icon name="warn" size={12} /> {confirm.sim.safety_flags.join(" · ")}</p>
            )}
            <p class="muted">{MODE_META[confirm.mode]?.sub}</p>
          </>
        )}
      </Modal>

      {/* confirm: ActionDiff (replan / pause / resume) */}
      <Modal
        open={confirm != null && confirm.kind !== "mode"}
        onClose={() => setConfirm(null)}
        width="sm"
        title={
          confirm?.kind === "replan" ? "Replan now?"
          : confirm?.kind === "pause" ? "Pause the scheduler?"
          : "Resume the scheduler?"
        }
        footer={
          confirm != null && confirm.kind !== "mode" && (
            <>
              <button class="btn btn--ghost" disabled={busy} onClick={() => setConfirm(null)}>Cancel</button>
              <button class="btn btn--primary" disabled={busy} onClick={() => confirmDiffAction(confirm)}>
                {busy ? "Working…" : "Confirm"}
              </button>
            </>
          )
        }
      >
        {confirm != null && confirm.kind !== "mode" && (
          <>
            <p>{confirm.diff.human_summary || "No diff summary available."}</p>
            {confirm.diff.safety_flags.length > 0 && (
              <p class="operate-warnings">
                <Icon name="warn" size={12} /> {confirm.diff.safety_flags.join(" · ")}
              </p>
            )}
            {confirm.diff.cost_delta_pence != null && (
              <p class="muted">
                Estimated cost impact: {confirm.diff.cost_delta_pence >= 0 ? "+" : "−"}
                {Math.abs(confirm.diff.cost_delta_pence).toFixed(1)}p
              </p>
            )}
          </>
        )}
      </Modal>
    </section>
  );
}
