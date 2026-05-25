import { useState } from "preact/hooks";
import type { SettingSpec } from "../../lib/types";
import type { GroupSpec } from "./groups";
import { SettingField } from "./SettingField";
import { Card } from "../common/Card";
import "./settings.css";

interface SettingGroupProps {
  group: GroupSpec;
  specs: SettingSpec[];
  pending: Record<string, unknown>;
  onChange: (key: string, value: unknown) => void;
  onRevert: (key: string) => void;
}

export function SettingGroup({ group, specs, pending, onChange, onRevert }: SettingGroupProps) {
  const [open, setOpen] = useState(group.expanded);

  const pendingCount = specs.reduce(
    (n, s) => n + (s.key in pending && pending[s.key] !== s.value ? 1 : 0),
    0,
  );
  const overrideCount = specs.reduce((n, s) => n + (s.overridden ? 1 : 0), 0);

  return (
    <Card
      class="setting-group"
      pad="tight"
      title={
        <button
          type="button"
          class={`setting-group-head${open ? " is-open" : ""}`}
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          <span class="setting-group-caret" aria-hidden="true">▸</span>
          <span class="setting-group-title">{group.title}</span>
          {pendingCount > 0 && (
            <span class="setting-group-badge setting-group-badge--pending">{pendingCount} pending</span>
          )}
          {overrideCount > 0 && pendingCount === 0 && (
            <span class="setting-group-badge setting-group-badge--override">{overrideCount} override{overrideCount === 1 ? "" : "s"}</span>
          )}
          <span class="setting-group-subtitle">{group.subtitle}</span>
        </button>
      }
    >
      {open && (
        <div class="setting-group-fields">
          {specs.map((spec) => (
            <SettingField
              key={spec.key}
              spec={spec}
              pending={pending[spec.key]}
              onChange={onChange}
              onRevert={onRevert}
            />
          ))}
        </div>
      )}
    </Card>
  );
}
