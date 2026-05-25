import type { SettingSpec } from "../../lib/types";
import { NumberInput, Select, Toggle, TextInput, SliderInput } from "../common/Inputs";
import { Pill } from "../common/Pill";
import { labelFor } from "./groups";
import "./settings.css";

interface SettingFieldProps {
  spec: SettingSpec;
  pending: unknown | undefined;
  onChange: (key: string, value: unknown) => void;
  onRevert: (key: string) => void;
}

function effectiveValue(spec: SettingSpec, pending: unknown | undefined): unknown {
  return pending !== undefined ? pending : spec.value;
}

function hasPendingEdit(spec: SettingSpec, pending: unknown | undefined): boolean {
  if (pending === undefined) return false;
  return pending !== spec.value;
}

// Unit guess from key suffix — keeps the slider readable without backend changes.
function unitFor(key: string): string {
  if (key.endsWith("_C")) return "°C";
  if (key.endsWith("_KWH")) return "kWh";
  if (key.endsWith("_MIN") || key.endsWith("_MINUTES") || key.endsWith("_MINUTE")) return "min";
  if (key.endsWith("_HOUR") || key.endsWith("_HOUR_LOCAL")) return "h";
  if (key.endsWith("_LPM")) return "L/m";
  if (key.endsWith("_PENCE_PER_KWH")) return "p/kWh";
  if (key.endsWith("_DAYS")) return "d";
  if (key.endsWith("_DAY")) return "";
  if (key.endsWith("_FRACTION") || key.endsWith("_PERCENT")) return "";
  return "";
}

function stepFor(spec: SettingSpec): number {
  if (spec.type === "int") return 1;
  // Float: pick reasonable step from range magnitude.
  if (spec.min != null && spec.max != null) {
    const range = spec.max - spec.min;
    if (range <= 1) return 0.01;
    if (range <= 10) return 0.1;
    if (range <= 100) return 0.5;
  }
  return 0.1;
}

export function SettingField({ spec, pending, onChange, onRevert }: SettingFieldProps) {
  const value = effectiveValue(spec, pending);
  const dirty = hasPendingEdit(spec, pending);
  const hasRange = (spec.type === "int" || spec.type === "float") && spec.min != null && spec.max != null;

  const inputEl = (() => {
    switch (spec.type) {
      case "int":
      case "float":
        if (hasRange) {
          return (
            <SliderInput
              value={value as number}
              min={spec.min as number}
              max={spec.max as number}
              step={stepFor(spec)}
              unit={unitFor(spec.key)}
              defaultValue={spec.default as number}
              ariaLabel={spec.key}
              onChange={(n) => onChange(spec.key, n)}
            />
          );
        }
        return (
          <NumberInput
            value={value as number}
            min={spec.min ?? null}
            max={spec.max ?? null}
            step={stepFor(spec)}
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

  return (
    <div class={`setting-field${dirty ? " is-dirty" : ""}${hasRange ? " has-slider" : ""}`}>
      <div class="setting-field-info">
        <div class="setting-field-label">
          <span class="setting-field-name">{labelFor(spec.key)}</span>
          <div class="setting-field-tags">
            {spec.overridden && !dirty && (
              <Pill tone="accent" title="This value differs from the .env default and is being driven by the runtime_settings table">
                Custom value
              </Pill>
            )}
            {dirty && (
              <Pill tone="warn" title="Edit staged locally — not yet applied to the server">
                Edited
              </Pill>
            )}
            {spec.cron_reload && (
              <Pill tone="dim" title="Changing this key hot-reloads the APScheduler cron jobs">
                cron-reload
              </Pill>
            )}
          </div>
        </div>
        <div class="setting-field-key">
          <code>{spec.key}</code>
          <span class="setting-field-default">
            default <strong>{String(spec.default)}</strong>
          </span>
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
