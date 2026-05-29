import type { GroupSpec } from "./groups";
import "./settings.css";

interface SettingsTabsProps {
  groups: GroupSpec[];
  activeId: string;
  pendingByGroup: Record<string, number>;
  onSelect: (id: string) => void;
}

// Horizontal tab strip — replaces the vertical accordion. Scrollable on
// mobile. Pending count badge appears on tabs with staged edits.
export function SettingsTabs({ groups, activeId, pendingByGroup, onSelect }: SettingsTabsProps) {
  return (
    <nav class="settings-tabs" aria-label="Setting groups" role="tablist">
      <div class="settings-tabs-inner">
        {groups.map((g, i) => {
          const prev = groups[i - 1];
          const showSeparator = prev && prev.advanced !== g.advanced;
          const pending = pendingByGroup[g.id] || 0;
          return (
            <>
              {showSeparator && <span class="settings-tabs-sep" aria-hidden="true" />}
              <button
                key={g.id}
                type="button"
                role="tab"
                aria-selected={g.id === activeId}
                class={`settings-tab${g.id === activeId ? " is-active" : ""}${g.advanced ? " is-advanced" : ""}`}
                onClick={() => onSelect(g.id)}
              >
                <span class="settings-tab-label">{g.title}</span>
                {pending > 0 && <span class="settings-tab-badge">{pending}</span>}
              </button>
            </>
          );
        })}
      </div>
    </nav>
  );
}
