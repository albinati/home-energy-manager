import "./settings.css";

interface BatchBarProps {
  pendingCount: number;
  busy: boolean;
  onSimulate: () => void;
  onDiscardAll: () => void;
}

export function BatchBar({ pendingCount, busy, onSimulate, onDiscardAll }: BatchBarProps) {
  const visible = pendingCount > 0;
  return (
    <div class={`batch-bar${visible ? " is-visible" : ""}`} role="region" aria-label="Pending changes">
      <div class="batch-bar-inner">
        <div class="batch-bar-count">
          <strong>{pendingCount}</strong>
          <span> pending change{pendingCount === 1 ? "" : "s"}</span>
        </div>
        <div class="batch-bar-actions">
          <button type="button" class="btn btn--ghost" onClick={onDiscardAll} disabled={busy}>
            Discard all
          </button>
          <button type="button" class="btn btn--primary" onClick={onSimulate} disabled={busy}>
            {busy ? "Working…" : "Simulate"}
          </button>
        </div>
      </div>
    </div>
  );
}
