import type { SettingSpec } from "../../lib/types";
import "./settings.css";

// The three household modes drive the entire DHW + dispatch policy. Make this
// the most obvious control on the settings page.

const MODE_META: Record<string, { label: string; sub: string; tone: string }> = {
  normal: {
    label: "Normal",
    sub: "Day-to-day. 4 showers/evening, standard arbitrage, DHW pinned to fixed schedule.",
    tone: "var(--ok)",
  },
  guests: {
    label: "Guests",
    sub: "Extra showers, tank held warm 24h, more conservative dispatch.",
    tone: "var(--warn)",
  },
  vacation: {
    label: "Vacation",
    sub: "DHW off, PV-only charging, maximum peak-export arbitrage.",
    tone: "var(--accent)",
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
  const meta = MODE_META[current] || { label: current, sub: "", tone: "var(--text-dim)" };

  return (
    <section class="mode-switcher" aria-label="Household mode">
      <header class="mode-switcher-head">
        <div>
          <div class="mode-switcher-eyebrow">Household mode</div>
          <h2 class="mode-switcher-title">
            <span class="mode-switcher-dot" style={{ background: meta.tone }} />
            {meta.label}
          </h2>
          <p class="mode-switcher-sub">{meta.sub}</p>
        </div>
        {dirty && (
          <div class="mode-switcher-dirty">
            <span>Pending: <strong>{current}</strong> (was {serverValue})</span>
            <button type="button" class="btn btn--ghost" onClick={onRevert}>Revert</button>
          </div>
        )}
      </header>
      <div class="mode-switcher-options" role="radiogroup" aria-label="Mode">
        {options.map((opt) => {
          const m = MODE_META[opt] || { label: opt, sub: "", tone: "var(--text-dim)" };
          const active = opt === current;
          return (
            <button
              key={opt}
              type="button"
              role="radio"
              aria-checked={active}
              class={`mode-option${active ? " is-active" : ""}`}
              onClick={() => onChange(opt)}
            >
              <span class="mode-option-dot" style={{ background: m.tone }} />
              <span class="mode-option-label">{m.label}</span>
              <span class="mode-option-sub">{m.sub}</span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
