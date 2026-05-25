import { Pill } from "../common/Pill";
import { pence, slotKindLabel, slotKindColorVar } from "../../lib/format";
import type { CockpitNow } from "../../lib/types";

interface DispatchReasonProps {
  now: CockpitNow;
  decisionReason?: string | null;
}

// "Why the system is doing what it's doing right now" — slot kind + price band +
// LP reason chip if available.
export function DispatchReason({ now, decisionReason }: DispatchReasonProps) {
  const slot = now.current_slot;
  const kind = slot.kind || mapKindFromMode(slot.fox_mode);
  const kindColor = slotKindColorVar(kind);

  return (
    <div class="dispatch-reason">
      <div class="dispatch-reason-row">
        <div class="dispatch-reason-kind">
          <span class="dispatch-reason-dot" style={{ background: kindColor }} />
          <span class="dispatch-reason-kind-label">{slotKindLabel(kind)}</span>
        </div>
        <div class="dispatch-reason-prices">
          <Pill tone={importTone(slot.price_import_p)} title="Import price">
            Import {pence(slot.price_import_p)}
          </Pill>
          <Pill tone={exportTone(slot.price_export_p)} title="Export price">
            Export {pence(slot.price_export_p)}
          </Pill>
          <Pill tone="dim" title="Fox V3 mode in this slot">
            {slot.fox_mode || "—"}
          </Pill>
        </div>
      </div>
      {decisionReason && (
        <p class="dispatch-reason-text">{decisionReason}</p>
      )}
    </div>
  );
}

function mapKindFromMode(mode: string | undefined): string {
  if (!mode) return "standard";
  const m = mode.toLowerCase();
  if (m.includes("force") && m.includes("charge")) return "cheap";
  if (m.includes("force") && m.includes("discharge")) return "peak_export";
  return "standard";
}

function importTone(p: number): "neutral" | "ok" | "warn" | "bad" | "accent" {
  if (p < 0) return "accent";
  if (p < 12) return "ok";
  if (p >= 28) return "warn";
  return "neutral";
}
function exportTone(p: number): "neutral" | "ok" | "warn" {
  if (p >= 15) return "ok";
  return "neutral";
}
