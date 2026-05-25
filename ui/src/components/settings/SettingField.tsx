import type { SettingSpec } from "../../lib/types";
import { NumberInput, Select, Toggle, TextInput } from "../common/Inputs";
import { Pill } from "../common/Pill";
import { labelFor } from "./groups";
import "./settings.css";

interface SettingFieldProps {
  spec: SettingSpec;
  pending: unknown | undefined;
  onChange: (key: string, value: unknown) => void;
  onRevert: (key: string) => void;
}

// Current effective value = pending edit if any, otherwise server value.
function effectiveValue(spec: SettingSpec, pending: unknown | undefined): unknown {
  return pending !== undefined ? pending : spec.value;
}

// True if pending is set AND differs from server value.
function hasPendingEdit(spec: SettingSpec, pending: unknown | undefined): boolean {
  if (pending === undefined) return false;
  return pending !== spec.value;
}

function rangeHint(spec: SettingSpec): string | null {
  const min = spec.min;
  const max = spec.max;
  if (min == null && max == null) return null;
  if (min != null && max != null) return `${min} – ${max}`;
  if (min != null) return `≥ ${min}`;
  return `≤ ${max}`;
}

export function SettingField({ spec, pending, onChange, onRevert }: SettingFieldProps) {
  const value = effectiveValue(spec, pending);
  const dirty = hasPendingEdit(spec, pending);

  const inputEl = (() => {
    switch (spec.type) {
      case "int":
      case "float":
        return (
          <NumberInput
            value={value as number}
            min={spec.min ?? null}
            max={spec.max ?? null}
            step={spec.type === "int" ? 1 : "any" as unknown as number}
            ariaLabel={spec.key}
            onChange={(n) => onChange(spec.key, n)}
          />
        );
      case "bool":
        return (
          <Toggle
            value={!!value}
            ariaLabel={spec.key}
            onChange={(v) => onChange(spec.key, v)}
          />
        );
      case "enum":
        return (
          <Select
            value={String(value)}
            options={spec.enum || []}
            ariaLabel={spec.key}
            onChange={(v) => onChange(spec.key, v)}
          />
        );
      case "str":
        return (
          <TextInput
            value={String(value)}
            ariaLabel={spec.key}
            onChange={(v) => onChange(spec.key, v)}
          />
        );
    }
  })();

  const range = rangeHint(spec);

  return (
    <div class={`setting-field${dirty ? " is-dirty" : ""}`}>
      <div class="setting-field-info">
        <div class="setting-field-label">
          <span class="setting-field-name">{labelFor(spec.key)}</span>
          <div class="setting-field-tags">
            {spec.overridden && !dirty && <Pill tone="accent">Override</Pill>}
            {dirty && <Pill tone="warn">Pending</Pill>}
            {spec.cron_reload && <Pill tone="dim" title="Changing this hot-reloads the cron schedule">cron-reload</Pill>}
          </div>
        </div>
        <div class="setting-field-key">
          <code>{spec.key}</code>
          {range && <span class="setting-field-range">range {range}</span>}
        </div>
        {spec.description && <div class="setting-field-desc">{spec.description}</div>}
      </div>
      <div class="setting-field-control">
        {inputEl}
        {dirty && (
          <button
            type="button"
            class="setting-field-revert"
            onClick={() => onRevert(spec.key)}
            title="Discard this pending change"
          >
            ↺
          </button>
        )}
      </div>
    </div>
  );
}
