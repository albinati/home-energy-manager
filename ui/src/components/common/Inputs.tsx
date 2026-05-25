import type { JSX } from "preact";
import "./inputs.css";

interface NumberInputProps {
  value: number | string;
  onChange: (n: number) => void;
  min?: number | null;
  max?: number | null;
  step?: number;
  placeholder?: string;
  ariaLabel?: string;
  invalid?: boolean;
}

export function NumberInput({
  value,
  onChange,
  min,
  max,
  step,
  placeholder,
  ariaLabel,
  invalid = false,
}: NumberInputProps) {
  const handleInput = (e: JSX.TargetedEvent<HTMLInputElement>) => {
    const raw = (e.currentTarget as HTMLInputElement).value;
    if (raw === "" || raw === "-") return;
    const n = Number(raw);
    if (Number.isFinite(n)) onChange(n);
  };
  return (
    <input
      type="number"
      class={`input input--number${invalid ? " is-invalid" : ""}`}
      value={value}
      onInput={handleInput}
      min={min ?? undefined}
      max={max ?? undefined}
      step={step ?? "any"}
      placeholder={placeholder}
      aria-label={ariaLabel}
      aria-invalid={invalid}
    />
  );
}

interface SliderInputProps {
  value: number;
  onChange: (n: number) => void;
  min: number;
  max: number;
  step?: number;
  unit?: string;
  ariaLabel?: string;
  defaultValue?: number;
}

// Combo slider + numeric input bound to the same value. Renders when a key
// has both min and max bounds. Shows min/max tick labels and (optionally) a
// marker at the env default so the user can see how far they're straying.
export function SliderInput({
  value,
  onChange,
  min,
  max,
  step = 1,
  unit,
  ariaLabel,
  defaultValue,
}: SliderInputProps) {
  const handleRange = (e: JSX.TargetedEvent<HTMLInputElement>) => {
    const n = Number((e.currentTarget as HTMLInputElement).value);
    if (Number.isFinite(n)) onChange(n);
  };
  const handleNumber = (e: JSX.TargetedEvent<HTMLInputElement>) => {
    const raw = (e.currentTarget as HTMLInputElement).value;
    if (raw === "" || raw === "-") return;
    const n = Number(raw);
    if (Number.isFinite(n)) onChange(n);
  };

  const pctOf = (v: number) => ((v - min) / (max - min)) * 100;
  const defaultPct =
    defaultValue != null && defaultValue >= min && defaultValue <= max
      ? pctOf(defaultValue)
      : null;

  return (
    <div class="slider-input">
      <div class="slider-input-row">
        <div class="slider-input-track-wrap">
          <input
            type="range"
            class="slider-input-range"
            min={min}
            max={max}
            step={step}
            value={value}
            onInput={handleRange}
            aria-label={ariaLabel}
          />
          {defaultPct != null && (
            <span
              class="slider-input-default-mark"
              style={{ left: `${defaultPct}%` }}
              title={`Default ${defaultValue}${unit ? " " + unit : ""}`}
            />
          )}
        </div>
        <input
          type="number"
          class="input input--number slider-input-number"
          min={min}
          max={max}
          step={step}
          value={value}
          onInput={handleNumber}
          aria-label={`${ariaLabel} (numeric)`}
        />
        {unit && <span class="slider-input-unit">{unit}</span>}
      </div>
      <div class="slider-input-ticks">
        <span>{min}</span>
        <span>{max}</span>
      </div>
    </div>
  );
}

interface SelectProps<T extends string> {
  value: T;
  options: ReadonlyArray<T | { value: T; label: string }>;
  onChange: (v: T) => void;
  ariaLabel?: string;
}

export function Select<T extends string>({ value, options, onChange, ariaLabel }: SelectProps<T>) {
  return (
    <select
      class="input input--select"
      value={value}
      onChange={(e) => onChange((e.currentTarget as HTMLSelectElement).value as T)}
      aria-label={ariaLabel}
    >
      {options.map((opt) => {
        const v = typeof opt === "string" ? opt : opt.value;
        const label = typeof opt === "string" ? opt : opt.label;
        return (
          <option key={v} value={v}>
            {label}
          </option>
        );
      })}
    </select>
  );
}

interface ToggleProps {
  value: boolean;
  onChange: (v: boolean) => void;
  ariaLabel?: string;
}

export function Toggle({ value, onChange, ariaLabel }: ToggleProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={value}
      aria-label={ariaLabel}
      class={`toggle${value ? " toggle--on" : ""}`}
      onClick={() => onChange(!value)}
    >
      <span class="toggle-thumb" aria-hidden="true" />
    </button>
  );
}

interface TextInputProps {
  value: string;
  onChange: (s: string) => void;
  placeholder?: string;
  ariaLabel?: string;
  invalid?: boolean;
}

export function TextInput({ value, onChange, placeholder, ariaLabel, invalid }: TextInputProps) {
  return (
    <input
      type="text"
      class={`input input--text${invalid ? " is-invalid" : ""}`}
      value={value}
      onInput={(e) => onChange((e.currentTarget as HTMLInputElement).value)}
      placeholder={placeholder}
      aria-label={ariaLabel}
      aria-invalid={invalid}
    />
  );
}
