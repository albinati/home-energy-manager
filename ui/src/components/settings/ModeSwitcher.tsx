import type { SettingSpec } from "../../lib/types";
import { Icon, type IconName } from "../common/Icon";
import "./settings.css";

// The three household modes drive the entire DHW + dispatch policy. The
// segmented control is the page's single most consequential control — the
// one sanctioned full-accent surface.

// Exported: the Operate card (cockpit) renders the same modes and must show
// the exact same labels/consequences — one source of truth for the copy.
export const MODE_META: Record<string, { label: string; sub: string; icon: IconName }> = {
  normal: {
    label: "Normal",
    sub: "Day-to-day. 4 showers/evening, standard arbitrage, DHW pinned to fixed schedule.",
    icon: "efficiency",
  },
  guests: {
    label: "Guests",
    sub: "Extra showers, tank held warm 24h, more conservative dispatch.",
    icon: "heating",
  },
  vacation: {
    label: "Vacation",
    sub: "DHW off, PV-only charging, maximum peak-export arbitrage.",
    icon: "export",
  },
};

interface ModeSwitcherProps {
  spec: SettingSpec;
  pendingValue: string | undefined;
  onChange: (value: string) => void;
  onRevert: () => void;
}

export function ModeSwitcher({ spec, pendingValue, onChange, onRevert }: ModeSwitcherProps) {
  const options = (spec.enum || ["normal", "guests", "vacation"]) as string[];
  const current = (pendingValue ?? (spec.value as string)) as string;
  const serverValue = spec.value as string;
  const dirty = pendingValue !== undefined && pendingValue !== serverValue;
  const meta = MODE_META[current] || { label: current, sub: "", icon: "settings" as IconName };
  // Thumb index from the active option's position in spec.enum — never
  // reorders/renames the modes (accuracy: emits the exact same string).
  const activeIdx = Math.max(0, options.indexOf(current));

  return (
    <section class="mode-switcher" aria-label="Household mode">
      <header class="mode-switcher-head">
        <div>
          <div class="mode-switcher-eyebrow">Household mode</div>
          <h2 class="mode-switcher-title">{meta.label}</h2>
          <p class="mode-consequence" key={current}>{meta.sub}</p>
        </div>
        {dirty && (
          <div class="mode-switcher-dirty">
            <span class="mode-switcher-dirty-icon"><Icon name="schedule" size={16} /></span>
            <span>Pending: <strong>{current}</strong> (was {serverValue})</span>
            <button type="button" class="btn btn--ghost" onClick={onRevert}>Revert</button>
          </div>
        )}
      </header>
      <div class="mode-segment" role="radiogroup" aria-label="Mode"
           style={`--seg-count:${options.length}; --seg-idx:${activeIdx};`}>
        <span class="mode-segment-thumb" aria-hidden="true" />
        {options.map((opt) => {
          const m = MODE_META[opt] || { label: opt, sub: "", icon: "settings" as IconName };
          const active = opt === current;
          return (
            <button
              key={opt}
              type="button"
              role="radio"
              aria-checked={active}
              class={`mode-segment-btn${active ? " is-active" : ""}`}
              onClick={() => onChange(opt)}
            >
              <span class="mode-segment-icon"><Icon name={m.icon} size={18} /></span>
              <span class="mode-segment-label">{m.label}</span>
            </button>
          );
        })}
      </div>
      <div class="mode-switcher-footnote">
        <span class="mode-switcher-footnote-icon"><Icon name="schedule" size={14} /></span>
        Scheduling a mode for a date range (e.g. guests next weekend, vacation in July) isn't built yet — mode changes are immediate. Tracked separately.
      </div>
    </section>
  );
}
