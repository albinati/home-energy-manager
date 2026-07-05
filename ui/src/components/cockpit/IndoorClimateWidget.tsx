import type { SensorDevice } from "../../lib/types";
import { Icon } from "../common/Icon";
import "./indoorClimate.css";

// A reading older than this (minutes) is treated as absent — mirrors the
// backend's INDOOR_SENSOR_STALE_MINUTES so the LP and the UI agree on "fresh".
const STALE_MIN = 30;

interface RoomRow {
  key: string;
  name: string;
  temp: number | null;
  hum: number | null;
  ageMin: number;
  stale: boolean;
}

function ageMinutes(iso: string | null | undefined): number {
  if (!iso) return Infinity;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? Infinity : (Date.now() - t) / 60_000;
}

// "just now" / "3m" / "2h" / "1d" — compact freshness, no seconds.
function relAge(min: number): string {
  if (!Number.isFinite(min)) return "—";
  if (min < 1) return "now";
  if (min < 60) return `${Math.round(min)}m`;
  if (min < 1440) return `${Math.round(min / 60)}h`;
  return `${Math.round(min / 1440)}d`;
}

interface Props {
  devices?: SensorDevice[];
  loading?: boolean;
}

export function IndoorClimateWidget({ devices, loading }: Props) {
  const list = devices ?? [];

  if (!list.length) {
    return (
      <div class="ic-empty">
        {loading ? "Loading sensors…" : "No indoor sensors reporting yet."}
      </div>
    );
  }

  const rows: RoomRow[] = list
    .map((d) => {
      const lr = d.latest;
      const ageMin = ageMinutes(lr?.received_at);
      return {
        key: d.device_key,
        name: d.room || d.device_id || d.mac || "sensor",
        temp: lr?.temp_c ?? null,
        hum: lr?.humidity_pct ?? null,
        ageMin,
        stale: ageMin > STALE_MIN,
      };
    })
    .sort((a, b) => a.name.localeCompare(b.name));

  const withTemp = rows.filter((r) => r.temp != null);
  const freshWithTemp = withTemp.filter((r) => !r.stale);
  // Mean over fresh sensors (matches the LP); fall back to last-known when all
  // stale so the glance still shows a number, dimmed + flagged.
  const basis = freshWithTemp.length ? freshWithTemp : withTemp;
  const meanTemp = basis.length
    ? basis.reduce((s, r) => s + (r.temp as number), 0) / basis.length
    : null;
  const allStale = freshWithTemp.length === 0;
  const newestAge = Math.min(...rows.map((r) => r.ageMin));
  const single = rows.length === 1;

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
              {rows[0].name}
              {rows[0].hum != null && <> · {Math.round(rows[0].hum)}% humidity</>}
            </>
          ) : (
            <>inside · {rows.length} rooms</>
          )}
          {allStale && <> · stale {relAge(newestAge)}</>}
        </div>
      </div>

      {!single && (
        <div class="ic-rooms">
          {rows.map((r) => (
            <div key={r.key} class={`ic-room ${r.stale ? "is-stale" : ""}`}>
              <span class="ic-room-name">
                <Icon name="thermometer" size={13} /> {r.name}
              </span>
              <span class="ic-room-temp">{r.temp != null ? `${r.temp.toFixed(1)}°` : "—"}</span>
              <span class="ic-room-hum">
                {r.hum != null ? (
                  <>
                    <Icon name="droplet" size={12} /> {Math.round(r.hum)}%
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
