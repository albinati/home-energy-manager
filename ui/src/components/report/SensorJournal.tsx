import { useFetch } from "../../lib/poll";
import { getSensorDeviceLog, getSensorDevices } from "../../lib/endpoints";
import { Spinner } from "../common/Spinner";
import { Pill } from "../common/Pill";
import { groupByDay, hhmm, relTime } from "../../lib/format";

// Sensor journal — the lossless per-device audit (#540 W1c): every reading a
// room sensor POSTed, straight from device_reading_log, plus a per-device
// overview with the latest metrics. Viewer / read-only.

const RANGES = [
  { hours: 6, label: "6h" },
  { hours: 24, label: "24h" },
  { hours: 72, label: "3 days" },
  { hours: 168, label: "7 days" },
];

// db.get_device_reading_log caps every response at this many newest rows; a
// full response means the window was cut short and the UI must say so.
const LOG_ROW_CAP = 5000;

// Payload fields already rendered elsewhere in the row (metrics columns,
// quality pill, identity) — anything ELSE a device sent (RSSI, battery, a 2nd
// temperature…) surfaces as an inline hint so nothing in the lossless log is
// invisible.
const KNOWN_FIELDS = new Set([
  "captured_at", "temp_c", "humidity_pct", "pressure_hpa",
  "room", "source", "device_id", "mac", "quality",
]);

function extraHint(payload: Record<string, unknown> | null): string | null {
  if (!payload) return null;
  const show = (v: unknown): string => {
    if (typeof v === "number") return String(Math.round(v * 100) / 100);
    if (typeof v === "object") {
      try { return JSON.stringify(v); } catch { return String(v); }
    }
    return String(v);
  };
  const bits = Object.entries(payload)
    .filter(([k, v]) => !KNOWN_FIELDS.has(k) && v != null)
    .map(([k, v]) => `${k}=${show(v)}`);
  return bits.length ? bits.join(" · ") : null;
}

// A device's own quality flag; surfaced as a warning pill unless it says fine.
function qualityFlag(payload: Record<string, unknown> | null): string | null {
  const q = payload?.quality;
  if (typeof q !== "string" || !q) return null;
  return ["good", "ok"].includes(q.toLowerCase()) ? null : q;
}

// Freshness tone off the device timestamp: green within the LP's ~30-min
// stale window, amber within a day, red beyond (sensor likely offline).
function ageTone(iso: string | null | undefined): "ok" | "warn" | "bad" {
  if (!iso) return "bad";
  const ageMin = (Date.now() - new Date(iso).getTime()) / 60000;
  if (!Number.isFinite(ageMin) || ageMin > 24 * 60) return "bad";
  return ageMin <= 30 ? "ok" : "warn";
}

function deviceTitle(d: { room: string | null; device_id: string | null; device_key: string }): string {
  return d.room || d.device_id || d.device_key;
}

function metricBits(r: {
  temp_c: number | null; humidity_pct: number | null; pressure_hpa: number | null;
}): string[] {
  const bits: string[] = [];
  if (r.temp_c != null) bits.push(`${r.temp_c.toFixed(1)}°C`);
  if (r.humidity_pct != null) bits.push(`${Math.round(r.humidity_pct)}% RH`);
  if (r.pressure_hpa != null) bits.push(`${Math.round(r.pressure_hpa)} hPa`);
  return bits;
}

interface SensorJournalProps {
  device: string | null;
  setDevice: (d: string | null) => void;
  hours: number;
  setHours: (h: number) => void;
}

export function SensorJournal({ device, setDevice, hours, setHours }: SensorJournalProps) {
  const devices = useFetch(() => getSensorDevices(), []);
  const log = useFetch(
    () => getSensorDeviceLog(device ?? undefined, hours),
    [device, hours],
  );

  const deviceList = devices.data?.devices ?? [];
  const rows = log.data?.rows ?? [];
  const truncated = (log.data?.n_rows ?? 0) >= LOG_ROW_CAP;
  // Only claim "no sensor has ever reported" when the devices fetch has
  // actually succeeded with zero devices — not while loading or on error.
  const noDeviceEver = !!devices.data && devices.data.n_devices === 0;

  const groups = groupByDay(rows, (r) => r.captured_at ?? r.received_at);
  const manyDevices = deviceList.length > 1;

  return (
    <div class="sensor-journal">
      {devices.error && (
        <p class="report-error">Couldn't load sensor devices: {devices.error.message}</p>
      )}
      {deviceList.length > 0 && (
        <div class="sensor-devices">
          {deviceList.map((d) => {
            const active = device === d.device_key;
            const seenAt = d.latest?.captured_at ?? d.last_seen;
            return (
              <button
                key={d.device_key}
                type="button"
                class={`sensor-device${active ? " active" : ""}`}
                aria-pressed={active}
                title={manyDevices ? "Filter the log to this device" : d.device_key}
                onClick={() => setDevice(active ? null : d.device_key)}
              >
                <span class="sensor-device-room">
                  <span class={`report-dot sensor-dot--${ageTone(seenAt)}`} />
                  {deviceTitle(d)}
                </span>
                <span class="sensor-device-metrics">
                  {d.latest ? metricBits(d.latest).join(" · ") : "—"}
                </span>
                <span class="sensor-device-meta">
                  {relTime(seenAt)} · {d.n_readings.toLocaleString()} readings
                  {d.source ? ` · ${d.source}` : ""}
                </span>
              </button>
            );
          })}
        </div>
      )}

      <div class="report-filters">
        <p class="muted sensor-count">
          {log.data ? `${log.data.n_rows.toLocaleString()} readings` : " "}
          {device ? " · filtered" : ""}
        </p>
        <div class="report-range">
          {RANGES.map((r) => (
            <button key={r.hours} class={`report-tab${hours === r.hours ? " active" : ""}`}
                    onClick={() => setHours(r.hours)}>{r.label}</button>
          ))}
        </div>
      </div>

      {truncated && (
        <p class="muted sensor-truncated">
          Showing the newest {LOG_ROW_CAP.toLocaleString()} readings — older rows
          in this window are cut off. Narrow the range or filter to one device
          for full coverage.
        </p>
      )}

      {log.loading && !log.data && <Spinner label="Loading sensor readings…" />}
      {log.error && <p class="report-error">Couldn't load the sensor log: {log.error.message}</p>}
      {!log.loading && !log.error && rows.length === 0 && (
        <p class="muted report-empty">
          {noDeviceEver
            ? "No sensor has reported yet — readings appear here as soon as a device POSTs to /api/v1/sensors/indoor."
            : "No readings in this window."}
        </p>
      )}

      {groups.map((g) => (
        <section key={g.day} class="report-group">
          <h2 class="report-group-day">{g.day}</h2>
          <ul class="report-list">
            {g.items.map((r) => {
              const extras = extraHint(r.payload);
              const quality = qualityFlag(r.payload);
              return (
                <li key={`${r.device_key}|${r.captured_at ?? r.received_at}`} class="report-row sensor-row">
                  <span class="report-time">{hhmm(r.captured_at ?? r.received_at)}</span>
                  <span class="report-dot sensor-dot--log" />
                  <span class="report-device">{deviceTitle(r)}</span>
                  <span class="report-action">
                    {metricBits(r).join(" · ") || "no metrics"}
                    {extras && <span class="report-hint"> · {extras}</span>}
                  </span>
                  <span class="report-meta">
                    {quality && (
                      <Pill tone="warn" title="Quality flag reported by the device itself">
                        {quality}
                      </Pill>
                    )}
                    {!r.captured_at && (
                      <span class="report-trigger" title="Device sent no timestamp; showing server receipt time">
                        server time
                      </span>
                    )}
                    {r.source && <span class="report-trigger">{r.source}</span>}
                  </span>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}
