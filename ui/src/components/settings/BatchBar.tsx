import "./settings.css";

type BatchStage = "edit" | "simulate" | "apply";

interface BatchBarProps {
  pendingCount: number;
  busy: boolean;
  stage: BatchStage;
  onSimulate: () => void;
  onDiscardAll: () => void;
}

// Sticky bottom bar that walks the user through the 3-step flow:
//   1. Edit          (they're tweaking values; pendingCount > 0)
//   2. Simulate      (modal open; reviewing diff + warnings + sim id)
//   3. Apply         (committing with X-Simulation-Id; on success, clears)
// The current stage is highlighted; future stages are dim; past stages tick.
export function BatchBar({ pendingCount, busy, stage, onSimulate, onDiscardAll }: BatchBarProps) {
  const visible = pendingCount > 0 || stage !== "edit";
  return (
    <div class={`batch-bar${visible ? " is-visible" : ""}`} role="region" aria-label="Pending changes">
      <div class="batch-bar-inner">
        <div class="batch-stages" aria-label="Apply flow">
          <Stage n={1} label="Edit" sub={pendingCount === 0 ? "no changes" : `${pendingCount} change${pendingCount === 1 ? "" : "s"}`}
                 active={stage === "edit"} done={stage !== "edit"} />
          <span class="batch-stages-arrow">→</span>
          <Stage n={2} label="Simulate" sub="dry-run the diff" active={stage === "simulate"} done={stage === "apply"} />
          <span class="batch-stages-arrow">→</span>
          <Stage n={3} label="Apply" sub="write to runtime_settings" active={stage === "apply"} done={false} />
        </div>
        <div class="batch-bar-actions">
          <button type="button" class="btn btn--ghost" onClick={onDiscardAll} disabled={busy || pendingCount === 0}>
            Discard all
          </button>
          <button type="button" class="btn btn--primary" onClick={onSimulate} disabled={busy || pendingCount === 0}>
            {busy ? "Working…" : "Simulate →"}
          </button>
        </div>
      </div>
    </div>
  );
}

function Stage({ n, label, sub, active, done }: { n: number; label: string; sub: string; active: boolean; done: boolean }) {
  return (
    <div class={`batch-stage${active ? " is-active" : ""}${done ? " is-done" : ""}`}>
      <div class="batch-stage-num">{done ? "✓" : n}</div>
      <div class="batch-stage-text">
        <div class="batch-stage-label">{label}</div>
        <div class="batch-stage-sub">{sub}</div>
      </div>
    </div>
  );
}
