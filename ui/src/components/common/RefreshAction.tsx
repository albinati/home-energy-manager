import "./refresh-action.css";

interface RefreshActionProps {
  onRefresh: () => void | Promise<void>;
  loading?: boolean;
  title?: string;
}

export function RefreshAction({ onRefresh, loading, title }: RefreshActionProps) {
  return (
    <button
      class={`refresh-action${loading ? " refresh-action--loading" : ""}`}
      onClick={() => { void onRefresh(); }}
      disabled={loading}
      title={title ?? "Re-fetch (cache-only — no cloud call)"}
      aria-label="Refresh"
    >
      <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
        <path
          d="M13.5 8a5.5 5.5 0 1 1-1.61-3.89M13.5 2.5v3h-3"
          fill="none"
          stroke="currentColor"
          stroke-width="1.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </svg>
    </button>
  );
}
