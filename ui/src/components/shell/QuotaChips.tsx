import { useFetch } from "../../lib/poll";
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
  const used = data.quota_used_24h ?? 0;
  const budget = data.daily_budget ?? 0;
  const blocked = data.blocked === true;
  const pct = budget > 0 ? (used / budget) * 100 : 0;
  let tone: "ok" | "warn" | "bad" = "ok";
  if (blocked || pct >= 90) tone = "bad";
  else if (pct >= 60) tone = "warn";

  return (
    <span class={`quota-chip quota-chip--${tone}`}
          title={`${label}: ${used}/${budget} API calls used in last 24h${blocked ? " (BLOCKED)" : ""}`}>
      <span class="quota-chip-label">{label}</span>
      <span class="quota-chip-value">{used}/{budget}</span>
    </span>
  );
}
