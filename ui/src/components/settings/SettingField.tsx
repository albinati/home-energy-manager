import type { SettingSpec } from "../../lib/types";
import { NumberInput, Select, Toggle, TextInput, SliderInput } from "../common/Inputs";
import { Pill } from "../common/Pill";
import { Icon } from "../common/Icon";
import { labelFor, unitFor, PLAIN_COPY } from "./groups";
import "./settings.css";

interface SettingFieldProps {
  spec: SettingSpec;
  pending: unknown | undefined;
  onChange: (key: string, value: unknown) => void;
  onRevert: (key: string) => void;
  /** Visible groups: plain-words hint instead of the developer description, key line hidden. */
  plain?: boolean;
}

function effectiveValue(spec: SettingSpec, pending: unknown | undefined): unknown {
  return pending !== undefined ? pending : spec.value;
}

function hasPendingEdit(spec: SettingSpec, pending: unknown | undefined): boolean {
  if (pending === undefined) return false;
  return pending !== spec.value;
}

function stepFor(spec: SettingSpec): number {
  if (spec.type === "int") return 1;
  if (spec.min != null && spec.max != null) {
    const range = spec.max - spec.min;
    if (range <= 1) return 0.01;
    if (range <= 10) return 0.1;
    if (range <= 100) return 0.5;
  }
  return 0.1;
}

export function SettingField({ spec, pending, onChange, onRevert, plain }: SettingFieldProps) {
  const value = effectiveValue(spec, pending);
  const dirty = hasPendingEdit(spec, pending);
  const hasRange = (spec.type === "int" || spec.type === "float") && spec.min != null && spec.max != null;
  const unit = unitFor(spec.key);

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
              unit={unit}
              defaultValue={spec.default as number}
              ariaLabel={labelFor(spec.key)}
              onChange={(n) => onChange(spec.key, n)}
            />
          );
        }
        return (
          <div class="setting-input-with-unit">
            <NumberInput
              value={value as number}
              min={spec.min ?? null}
              max={spec.max ?? null}
              step={stepFor(spec)}
              ariaLabel={labelFor(spec.key)}
              onChange={(n) => onChange(spec.key, n)}
            />
            {unit && <span class="setting-input-unit">{unit}</span>}
          </div>
        );
      case "bool":
        return (
          <Toggle
            value={!!value}
            ariaLabel={labelFor(spec.key)}
            onChange={(v) => onChange(spec.key, v)}
          />
        );
      case "enum":
        return (
          <Select
            value={String(value)}
            options={spec.enum || []}
            ariaLabel={labelFor(spec.key)}
            onChange={(v) => onChange(spec.key, v)}
          />
        );
      case "str":
        return (
          <TextInput
            value={String(value)}
            ariaLabel={labelFor(spec.key)}
            onChange={(v) => onChange(spec.key, v)}
          />
        );
    }
  })();

  const defaultDisplay = `${String(spec.default)}${unit ? " " + unit : ""}`;

  return (
    <div class={`setting-field${dirty ? " is-dirty" : ""}${hasRange ? " has-slider" : ""}`}>
      <div class="setting-field-info">
        <div class="setting-field-label">
          <span class="setting-field-name">{labelFor(spec.key)}</span>
          <div class="setting-field-tags">
            {spec.overridden && !dirty && (
              <Pill tone="accent" title="Valor diferente do padrão do .env, vindo da tabela runtime_settings">
                Alterado
              </Pill>
            )}
            {dirty && (
              <Pill tone="warn" title="Edição local, ainda não aplicada">
                Editado
              </Pill>
            )}
            {spec.cron_reload && (
              <Pill tone="dim" title="Changing this key hot-reloads the APScheduler cron jobs (no restart needed)">
                hot-reload
              </Pill>
            )}
          </div>
        </div>
        {plain ? (
          <div class="setting-field-key">
            <span class="setting-field-default">padrão <strong>{defaultDisplay}</strong></span>
          </div>
        ) : (
          <div class="setting-field-key">
            <code>{spec.key}</code>
            <span class="setting-field-default">padrão <strong>{defaultDisplay}</strong></span>
          </div>
        )}
        {plain && PLAIN_COPY[spec.key]
          ? <div class="setting-field-desc">{PLAIN_COPY[spec.key].hint}</div>
          : spec.description && <div class="setting-field-desc">{spec.description}</div>}
      </div>
      <div class="setting-field-control">
        {inputEl}
        {dirty && (
          <button
            type="button"
            class="setting-field-revert"
            onClick={() => onRevert(spec.key)}
            title="Descartar esta alteração"
            aria-label="Desfazer"
          >
            <Icon name="revert" size={16} />
          </button>
        )}
      </div>
    </div>
  );
}
