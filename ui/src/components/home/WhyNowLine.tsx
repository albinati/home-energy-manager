import type { TimelineSlot } from "../../lib/types";
import { whyNowPhrase } from "../../lib/planHelpers";
import "./why-now.css";

// One glanceable sentence under the Live power flow: what the battery/inverter
// is doing RIGHT NOW and why. Rides the /scheduler/timeline + /cockpit/now
// polls the card already runs (no extra fetch). Coloured dot carries the
// semantic tint (hold = neutral, cheap = --cheap, peak = --warn, solar = --ok).
export function WhyNowLine({ ongoing, foxMode }: { ongoing?: TimelineSlot | null; foxMode?: string }) {
  const why = whyNowPhrase(ongoing, foxMode);
  if (!why) return null;
  return (
    <p class="why-now" title="Current battery/inverter state and the reason for it, from the committed plan.">
      <span class="why-now-dot" style={{ background: why.tone }} aria-hidden="true" />
      <span class="why-now-txt">{why.text}</span>
    </p>
  );
}
