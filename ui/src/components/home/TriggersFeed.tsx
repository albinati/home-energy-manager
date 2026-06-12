import { useFetch } from "../../lib/poll";
import { getRecentTriggers } from "../../lib/endpoints";
import { Icon } from "../common/Icon";
import "./feedback.css";

// Last few meaningful action_log events (manual writes, plan proposes,
// scheduler crons — heartbeat/notification noise already filtered server-side).
// ADMIN-ONLY: /recent-triggers is in the middleware's admin_read_prefixes, so
// the parent must role-gate the mount — this component assumes it IS admin and
// would just render its error-empty state otherwise.

function timeLabel(ts: string): string {
  try {
    const d = new Date(ts.includes("T") ? ts : ts.replace(" ", "T") + "Z");
    if (Number.isNaN(d.getTime())) return ts;
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch {
    return ts;
  }
}

export function TriggersFeed() {
  const triggers = useFetch(() => getRecentTriggers(6), []);
  const rows = triggers.data?.rows ?? [];
  if (triggers.error || rows.length === 0) return null;

  return (
    <div class="triggers-feed">
      <div class="triggers-feed-head">Recent actions</div>
      <ul class="triggers-feed-list">
        {rows.map((r) => {
          const failed = !!r.error_msg;
          return (
            <li key={r.id} class="triggers-feed-row" title={r.error_msg ?? r.result ?? undefined}>
              <span class="triggers-feed-time">{timeLabel(r.timestamp)}</span>
              <span class={`triggers-feed-status ${failed ? "is-warn" : "is-ok"}`} aria-hidden="true">
                <Icon name={failed ? "warn" : "check"} size={11} />
              </span>
              <span class="triggers-feed-what">
                {r.device ? `${r.device} · ` : ""}{r.action ?? "—"}
              </span>
              <span class="triggers-feed-meta">
                {r.trigger ?? ""}
                {r.duration_ms != null ? ` · ${r.duration_ms >= 1000 ? `${(r.duration_ms / 1000).toFixed(1)}s` : `${r.duration_ms}ms`}` : ""}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
