import type { SettingSpec } from "../../lib/types";
import { Icon, type IconName } from "../common/Icon";
import "./settings.css";

// Household mode drives the entire DHW + dispatch policy. The Home/Settings
// toggle offers normal | vacation only (#873); "guests" still exists in the
// backend enum and stays reachable under Settings > Vacation & guests
// (advanced). The segmented control is the page's single most consequential
// control - the one sanctioned full-accent surface.

// Exported: the Operate card (cockpit) renders the same modes and must show
// the exact same labels/consequences - one source of truth for the copy.
export const MODE_META: Record<string, { label: string; sub: string; icon: IconName }> = {
  normal: {
    label: "Normal",
    sub: "Em casa: rotina normal, aquecimento e tanque planejados pelo LP.",
    icon: "efficiency",
  },
  guests: {
    label: "Guests",
    sub: "Extra showers, tank held warm 24h, more conservative dispatch.",
    icon: "heating",
  },
  vacation: {
    label: "Vacation",
    sub: "Fora de casa: Daikin em holiday, bateria normal, carga reduzida.",
    icon: "export",
  },
};

// Modes offered by the primary toggles (Home + Settings header).
export const PRIMARY_MODES = ["normal", "vacation"];

// The stored return time is lower-cased by the settings layer ("2026-11-07t18:00",
// possibly with seconds/offset); the datetime-local input wants "YYYY-MM-DDTHH:mm".
// Compare values THROUGH this function on both sides so an untouched value never
// reads as a change (a re-submit re-activates the vacation).
export function toDatetimeLocal(v: unknown): string {
  const s = String(v ?? "").trim();
  return s ? s.replace(/t/i, "T").slice(0, 16) : "";
}

interface ModeSwitcherProps {
  spec: SettingSpec;
  pendingValue: string | undefined;
  onChange: (value: string) => void;
  onRevert: () => void;
  // #873: vacation needs a return time (VACATION_RETURN_AT, local datetime).
  returnAt?: string;
  onReturnAtChange?: (value: string) => void;
}

export function ModeSwitcher({ spec, pendingValue, onChange, onRevert, returnAt, onReturnAtChange }: ModeSwitcherProps) {
  const current = (pendingValue ?? (spec.value as string)) as string;
  // normal | vacation; a stored "guests" stays visible so the control never lies.
  const all = (spec.enum || ["normal", "guests", "vacation"]) as string[];
  const options = all.filter((o) => PRIMARY_MODES.includes(o) || o === current);
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
      {current === "vacation" && onReturnAtChange && (
        <label class="mode-switcher-return">
          <span class="mode-switcher-eyebrow">Volta em</span>
          <input
            type="datetime-local"
            value={toDatetimeLocal(returnAt)}
            onInput={(e) => onReturnAtChange((e.currentTarget as HTMLInputElement).value)}
            aria-label="Vacation return date and time"
          />
        </label>
      )}
    </section>
  );
}
