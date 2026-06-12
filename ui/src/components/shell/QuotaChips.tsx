import { useFetch } from "../../lib/poll";
import { Icon } from "../common/Icon";
import { getDaikinQuota, getFoxQuota } from "../../lib/endpoints";
import type { ApiQuotaResponse } from "../../lib/types";
import "./quota-chips.css";

// Two compact quota chips for Fox + Daikin shown in the footer. Helps the
// operator see budget before manually refreshing anything that hits the
// live cloud. Fetched once on mount — quota tables update every call so
// they're roughly accurate; no need to poll.
export function QuotaChips() {
  const fox = useFetch(getFoxQuota, []);
  const daikin = useFetch(getDaikinQuota, []);

  return (
    <div class="quota-chips">
      <QuotaChip label="Fox" data={fox.data} />
      <QuotaChip label="Daikin" data={daikin.data} />
    </div>
  );
}

function QuotaChip({ label, data }: { label: string; data: ApiQuotaResponse | null }) {
  if (!data) return <span class="quota-chip quota-chip--loading">{label} —</span>;
  // Prefer "today since midnight UTC" — that's what the upstream vendor
  // actually enforces. Fall back to rolling-24h when the backend is older.
  const usedToday = data.quota_used_today_utc;
  const used = usedToday ?? data.quota_used_24h ?? 0;
  const used24h = data.quota_used_24h ?? 0;
  const failed = data.quota_failed_24h ?? 0;
  const budget = data.daily_budget ?? 0;
  const blocked = data.blocked === true;
  const pct = budget > 0 ? (used / budget) * 100 : 0;
  let tone: "ok" | "warn" | "bad" = "ok";
  if (blocked || pct >= 90) tone = "bad";
  else if (pct >= 60) tone = "warn";

  const windowLabel = usedToday != null ? "today" : "24h";
  const tooltipParts = [
    `${label}: ${used}/${budget} (${windowLabel})`,
    usedToday != null ? `rolling 24h: ${used24h}` : null,
    failed > 0 ? `failed in 24h: ${failed}` : null,
    blocked ? "BLOCKED — soft cap reached" : null,
  ].filter(Boolean);

  return (
    <span class={`quota-chip quota-chip--${tone}`} title={tooltipParts.join(" · ")}>
      <span class="quota-chip-label">{label}</span>
      <span class="quota-chip-value">{used}/{budget}</span>
      {failed > 0 && <span class="quota-chip-fail" title={`${failed} failed calls in last 24h`}><Icon name="warn" size={11} />{failed}</span>}
    </span>
  );
}
