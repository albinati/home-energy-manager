import type { IndoorSummary } from "../../lib/types";
import { Icon } from "../common/Icon";
import "./indoorClimate.css";

// "just now" / "3m" / "2h" / "1d" — compact freshness, no seconds.
function relAge(min: number | null): string {
  if (min == null || !Number.isFinite(min)) return "—";
  if (min < 1) return "now";
  if (min < 60) return `${Math.round(min)}m`;
  if (min < 1440) return `${Math.round(min / 60)}h`;
  return `${Math.round(min / 1440)}d`;
}

interface Props {
  // The indoor snapshot folded into /cockpit/now — same source path as Fox +
  // tank, no separate poll.
  summary?: IndoorSummary | null;
}

export function IndoorClimateWidget({ summary }: Props) {
  const rooms = summary?.rooms ?? [];
  if (!rooms.length) {
    return <div class="ic-empty">No indoor sensors reporting yet.</div>;
  }

  const allStale = !!summary?.stale;
  // Prefer the server's fresh mean; fall back to a mean over any room with a
  // reading so the glance still shows a (dimmed) number when all are stale.
  const withTemp = rooms.filter((r) => r.temp_c != null);
  const fallbackMean = withTemp.length
    ? withTemp.reduce((s, r) => s + (r.temp_c as number), 0) / withTemp.length
    : null;
  const meanTemp = summary?.mean_c ?? fallbackMean;
  const newestAge = rooms.reduce<number | null>((min, r) => {
    if (r.age_min == null) return min;
    return min == null ? r.age_min : Math.min(min, r.age_min);
  }, null);
  const single = rooms.length === 1;

  return (
    <div class="ic">
      <div class="ic-focal">
        <div class={`ic-temp ${allStale ? "is-stale" : ""}`}>
          {meanTemp != null ? meanTemp.toFixed(1) : "—"}
          <span class="ic-deg">°</span>
        </div>
        <div class="ic-sub">
          <span class={`ic-dot ${allStale ? "is-stale" : "is-live live-pulse"}`} aria-hidden="true" />
          {single ? (
            <>
              {rooms[0].room}
              {rooms[0].humidity_pct != null && <> · {Math.round(rooms[0].humidity_pct)}% humidity</>}
            </>
          ) : (
            <>inside · {rooms.length} rooms</>
          )}
          {allStale && <> · stale {relAge(newestAge)}</>}
        </div>
      </div>

      {!single && (
        <div class="ic-rooms">
          {rooms.map((r) => (
            <div key={r.room} class={`ic-room ${r.stale ? "is-stale" : ""}`}>
              <span class="ic-room-name">
                <Icon name="thermometer" size={13} /> {r.room}
              </span>
              <span class="ic-room-temp">{r.temp_c != null ? `${r.temp_c.toFixed(1)}°` : "—"}</span>
              <span class="ic-room-hum">
                {r.humidity_pct != null ? (
                  <>
                    <Icon name="droplet" size={12} /> {Math.round(r.humidity_pct)}%
                  </>
                ) : (
                  ""
                )}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
