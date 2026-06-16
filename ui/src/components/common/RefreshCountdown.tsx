import { useEffect, useState } from "preact/hooks";
import "./refresh-countdown.css";

interface Props {
  // From a usePoll() result: when the last fetch landed + the poll interval.
  // Can also represent a cooldown window (lastFetchAt = cooldown start).
  lastFetchAt: number | null;
  intervalMs: number;
  loading?: boolean;   // a fetch is in flight → spin
  disabled?: boolean;  // not clickable (e.g. on cooldown) but not spinning
  onRefresh?: () => void;
  label?: string;
}

// A tiny ring that drains as the next auto-refresh approaches, with the seconds
// remaining inside. Click to refresh now. Mirrors the data the cockpit already
// polls on — purely presentational over usePoll's lastFetchAt/intervalMs.
export function RefreshCountdown({ lastFetchAt, intervalMs, loading, disabled, onRefresh, label }: Props) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  if (!intervalMs || lastFetchAt == null) {
    return (
      <button class="refresh-cd refresh-cd--plain" onClick={onRefresh} title="Refresh now" disabled={loading || disabled}>
        <span class={`refresh-cd-glyph${loading ? " spin" : ""}`}>↻</span>
        {label && <span class="refresh-cd-label">{label}</span>}
      </button>
    );
  }

  const nextAt = lastFetchAt + intervalMs;
  const leftMs = Math.max(0, nextAt - now);
  const leftS = Math.ceil(leftMs / 1000);
  const frac = Math.max(0, Math.min(1, leftMs / intervalMs)); // 1 → just refreshed
  const R = 8, C = 2 * Math.PI * R;
  const dash = C * frac;

  return (
    <button class="refresh-cd" onClick={onRefresh}
            aria-label={disabled ? `Just refreshed — available again in ${leftS}s` : "Refresh now"}
            title={disabled
              ? `Just refreshed — available again in ${leftS}s`
              : `Auto-refreshes every ${Math.round(intervalMs / 1000)}s — click to refresh now`}
            disabled={loading || disabled}>
      <svg width="22" height="22" viewBox="0 0 22 22" class={loading ? "spin" : ""} aria-hidden="true">
        <circle cx="11" cy="11" r={R} fill="none" stroke="var(--border-strong)" stroke-width="2" />
        <circle cx="11" cy="11" r={R} fill="none" stroke="var(--accent)" stroke-width="2"
                stroke-linecap="round" stroke-dasharray={`${dash} ${C}`}
                transform="rotate(-90 11 11)" style={{ transition: "stroke-dasharray 1s linear" }} />
      </svg>
      <span class="refresh-cd-num">{loading ? "↻" : leftS}</span>
      {label && <span class="refresh-cd-label">{label}</span>}
    </button>
  );
}
