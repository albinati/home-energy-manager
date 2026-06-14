import { usePoll } from "../../lib/poll";
import { getStatusAlerts } from "../../lib/endpoints";
import { cockpitFreshness, cockpitFreshnessAt } from "../../lib/freshness";
import { Icon } from "../common/Icon";
import type { StatusAlertsResponse } from "../../lib/types";
import "./alert-strip.css";

// Anomaly strip mounted between the TopNav and the page body on EVERY route.
// Design contract: when the system is healthy it renders NOTHING — zero
// pixels, zero layout shift. Chips only appear for conditions an operator
// should react to. /status/alerts is TTL-cached server-side (60s, vendor
// reads behind longer sub-caches), so the 60s client poll is cheap and can
// never amplify into Fox/Daikin quota.
//
// Failure posture: any fetch error (404 on an older hem image, network blip,
// auth misconfig) renders nothing. The strip is an enhancement — it must
// never be the thing that breaks the page.

interface Chip {
  key: string;
  tone: "bad" | "warn";
  label: string;
  detail?: string;
}

function buildChips(a: StatusAlertsResponse): Chip[] {
  const chips: Chip[] = [];

  if (a.meter?.stale) {
    chips.push({
      key: "meter",
      tone: "bad",
      label: a.meter.age_days != null ? `Meter data ${a.meter.age_days}d old` : "Meter data missing",
      detail: a.meter.last_day
        ? `Last metered day from Octopus: ${a.meter.last_day}. Bill figures beyond it are Fox-estimated.`
        : "No Octopus consumption data recorded yet.",
    });
  }

  if ((a.lp?.failures_24h ?? 0) > 0) {
    const lf = a.lp.last_failure;
    chips.push({
      key: "lp",
      tone: "bad",
      label: `LP solve failed ×${a.lp.failures_24h}`,
      detail: lf
        ? `Last: ${lf.error_class ?? "unknown"} at ${lf.run_at_utc ?? "?"} (plan ${lf.plan_date ?? "?"})`
        : undefined,
    });
  }

  const f = a.forecast;
  if (f?.degraded) {
    let label = "PV forecast degraded";
    if (f.sidecar_ok === false) label = "Quartz sidecar down";
    else if (f.model_name !== "quartz-open-site") label = `PV forecast: ${f.model_name ?? "unknown"} fallback`;
    else if (f.age_s != null && f.age_s > 7200) label = `PV forecast ${Math.round(f.age_s / 3600)}h old`;
    chips.push({
      key: "forecast",
      tone: "warn",
      label,
      detail: `model=${f.model_name ?? "?"} · fetched ${f.fetched_at_utc ?? "?"} · sidecar ${f.sidecar_ok == null ? "n/a" : f.sidecar_ok ? "ok" : "down"}`,
    });
  }

  if (a.fox_drift?.in_sync === false) {
    chips.push({
      key: "fox-drift",
      tone: "warn",
      label: `Fox schedule drift${a.fox_drift.diff_count != null ? ` (${a.fox_drift.diff_count})` : ""}`,
      detail: "Live inverter schedule differs from the last LP upload — manual app edit or failed upload. Checked every 30 min.",
    });
  }

  for (const [name, q] of [["Fox", a.quota?.fox], ["Daikin", a.quota?.daikin]] as const) {
    if (!q) continue;
    if (q.blocked) {
      chips.push({ key: `quota-${name}`, tone: "bad", label: `${name} quota blocked` });
    } else if (q.used != null && q.budget != null && q.budget > 0 && q.used / q.budget >= 0.9) {
      chips.push({
        key: `quota-${name}`,
        tone: "warn",
        label: `${name} quota ${q.used}/${q.budget}`,
        detail: "Over 90% of the daily vendor API budget used.",
      });
    }
  }

  // Actuation freshness — the plan not reaching the hardware. This is the gap
  // the 2026-06-14 ~41h Fox-upload wedge slipped through (drift was "in sync"
  // because live and stored were both stale).
  const act = a.actuation;
  if (act) {
    const hrs = (h: number | null | undefined) =>
      h == null ? "" : h >= 1 ? ` (${Math.round(h)}h)` : "";
    if (act.fox?.stale) {
      chips.push({
        key: "fox-stale",
        tone: "bad",
        label: `Fox plan not uploading${hrs(act.fox.age_hours)}`,
        detail: `No successful Fox V3 upload since ${act.fox.last_upload_at ?? "?"}. The inverter may be running an obsolete schedule.`,
      });
    }
    if (act.daikin_tank?.stale) {
      chips.push({
        key: "tank-stale",
        tone: "bad",
        label: `Tank not actuating${hrs(act.daikin_tank.age_hours)}`,
        detail: `No tank action has fired since ${act.daikin_tank.last_at ?? "?"} (normally ~2×/day). The DHW schedule may not be reaching the heat pump.`,
      });
    }
    if (act.daikin_tank?.failing) {
      chips.push({
        key: "tank-failing",
        tone: "warn",
        label: `Tank writes failing (${act.daikin_tank.failed_24h})`,
        detail: "Daikin rejected this many tank writes in 24h (READ_ONLY / rate-limit / clamp).",
      });
    }
    if (act.daikin_lwt?.failing) {
      chips.push({
        key: "lwt-failing",
        tone: "warn",
        label: `LWT writes failing (${act.daikin_lwt.failed_24h})`,
        detail: "Daikin rejected this many leaving-water-temp writes in 24h.",
      });
    }
  }

  return chips;
}

/** Staleness chip from the cockpit's own /cockpit/now poll, via the shared
 *  freshness signal — only meaningful while that poll is actually running
 *  (i.e. the user is on the cockpit and the map was published recently). */
function freshnessChip(): Chip | null {
  const at = cockpitFreshnessAt.value;
  const map = cockpitFreshness.value;
  if (!map || at == null || Date.now() - at > 2 * 60_000) return null;
  const stale = Object.entries(map).filter(([, v]) => v?.stale);
  if (stale.length === 0) return null;
  const names = stale.map(([k]) => k).join(", ");
  return {
    key: "freshness",
    tone: "warn",
    label: `Live data stale: ${names}`,
    detail: stale
      .map(([k, v]) => `${k}: ${v.age_s != null ? `${Math.round(v.age_s / 60)}m` : "?"} old`)
      .join(" · "),
  };
}

const DEBUG_CHIPS: Chip[] = [
  { key: "meter", tone: "bad", label: "Meter data 5d old", detail: "debugAlerts sample" },
  { key: "lp", tone: "bad", label: "LP solve failed ×2", detail: "debugAlerts sample" },
  { key: "forecast", tone: "warn", label: "PV forecast: open-meteo fallback", detail: "debugAlerts sample" },
  { key: "fox-drift", tone: "warn", label: "Fox schedule drift (3)", detail: "debugAlerts sample" },
];

export function AlertStrip() {
  const alerts = usePoll(getStatusAlerts, 60_000);

  const debug = typeof location !== "undefined" && location.search.includes("debugAlerts=1");
  let chips: Chip[] = debug ? DEBUG_CHIPS : [];
  if (!debug && alerts.data && !alerts.error) {
    chips = buildChips(alerts.data);
    const fc = freshnessChip();
    if (fc) chips.push(fc);
  }

  if (chips.length === 0) return null;

  return (
    <div class="alert-strip" role="status">
      {chips.map((c) => (
        <span key={c.key} class={`alert-chip alert-chip--${c.tone}`} title={c.detail}>
          <Icon name="warn" size={12} />
          {c.label}
        </span>
      ))}
    </div>
  );
}
