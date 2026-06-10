import type { JSX } from "preact";

// One coherent thin-line geometric icon family. 24×24 viewBox, 1.75px stroke
// (1.5px at ≤14px), round caps + joins, currentColor only — icons inherit the
// surrounding text colour and NEVER carry their own (the only sanctioned
// exceptions live inside the power-flow nodes + the Efficiency self-use
// banner, handled there). Replaces every emoji across the app.
//
// Usage: <Icon name="power-live" size={18} />

export type IconName =
  | "power-live"
  | "cost"
  | "export"
  | "import"
  | "heating"
  | "chart-bars"
  | "trend"
  | "battery"
  | "solar"
  | "grid"
  | "schedule"
  | "settings"
  | "efficiency"
  | "house"
  | "weather"
  | "appliance"
  | "chevron"
  | "check"
  | "revert"
  | "moon";

interface IconProps {
  name: IconName;
  size?: number;
  class?: string;
  title?: string;
  style?: string | JSX.CSSProperties;
}

// Each entry is the inner markup of the 24×24 viewBox. Stroke + caps come from
// the <svg> wrapper. `fill` stays none unless a path needs a dot/handle.
const PATHS: Record<IconName, JSX.Element> = {
  "power-live": (
    <>
      <path d="M13 3 L6 13 H11 L11 21 L18 10 H13 Z" />
      <circle cx="20" cy="4" r="1.4" fill="currentColor" stroke="none" />
    </>
  ),
  cost: <path d="M15 6.5 C15 4.5 13.3 3.5 11.5 3.5 C9.2 3.5 8 5 8 7.2 V18.5 M6 13 H13 M6 18.5 H16" />,
  export: (
    <>
      <path d="M5 15 V19 H19 V15" />
      <path d="M12 14 V4" />
      <path d="M8 8 L12 4 L16 8" />
    </>
  ),
  import: (
    <>
      <path d="M5 15 V19 H19 V15" />
      <path d="M12 4 V14" />
      <path d="M8 10 L12 14 L16 10" />
    </>
  ),
  heating: (
    <>
      <path d="M8 14 C7 12 9 11 8 9 C7 7 9 6 8 4" />
      <path d="M12 14 C11 12 13 11 12 9 C11 7 13 6 12 4" />
      <path d="M16 14 C15 12 17 11 16 9 C15 7 17 6 16 4" />
    </>
  ),
  "chart-bars": (
    <>
      <path d="M4 20 H20" />
      <path d="M7 20 V13" />
      <path d="M12 20 V8" />
      <path d="M17 20 V11" />
    </>
  ),
  trend: (
    <>
      <path d="M4 20 H20 M4 20 V4" />
      <path d="M5 16 L10 12 L13 14 L19 6" />
      <path d="M15 6 H19 V10" />
    </>
  ),
  battery: (
    <>
      <path d="M3 8 H17 a2 2 0 0 1 2 2 V14 a2 2 0 0 1 -2 2 H3 a2 2 0 0 1 -2 -2 V10 a2 2 0 0 1 2 -2 Z" />
      <path d="M21 11 V13" />
    </>
  ),
  solar: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 3 V5 M12 19 V21 M3 12 H5 M19 12 H21 M5.6 5.6 L7 7 M17 17 L18.4 18.4 M18.4 5.6 L17 7 M7 17 L5.6 18.4" />
    </>
  ),
  grid: (
    <>
      <path d="M6 21 L9 3 M18 21 L15 3 M9 3 H15" />
      <path d="M7.5 9 L16.5 9 M8 13 L16 13 M9 9 L15 13 M15 9 L9 13" />
    </>
  ),
  schedule: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 12 L12 7 M12 12 L15.5 13.5" />
    </>
  ),
  settings: (
    <>
      <path d="M3 7 H21 M3 12 H21 M3 17 H21" />
      <circle cx="8" cy="7" r="2.2" fill="var(--bg-card)" />
      <circle cx="15" cy="12" r="2.2" fill="var(--bg-card)" />
      <circle cx="7" cy="17" r="2.2" fill="var(--bg-card)" />
    </>
  ),
  efficiency: (
    <>
      <path d="M6 8 A8 8 0 0 1 18 7" />
      <path d="M18 4 L18 7 H15" />
      <path d="M18 16 A8 8 0 0 1 6 17" />
      <path d="M6 20 L6 17 H9" />
    </>
  ),
  house: (
    <>
      <path d="M5 11 L12 5 L19 11 V20 H5 Z" />
      <path d="M10 20 V15 H14 V20" />
    </>
  ),
  weather: (
    <>
      <circle cx="9" cy="8" r="3" />
      <path d="M9 3.2 V4.4 M4.2 8 H5.4 M5.6 4.6 L6.4 5.4 M12.4 4.6 L11.6 5.4" />
      <path d="M8 19 H16.5 A3 3 0 0 0 16.5 13 A3.4 3.4 0 0 0 10 12.6 A3.2 3.2 0 0 0 8 19 Z" />
    </>
  ),
  appliance: (
    <>
      <rect x="5" y="3.5" width="14" height="17" rx="2.5" />
      <circle cx="12" cy="13" r="4" />
      <path d="M8 6.5 H9 M11 6.5 H12" />
    </>
  ),
  chevron: <path d="M9 6 L15 12 L9 18" />,
  check: <path d="M5 12.5 L10 17 L19 7" />,
  revert: (
    <>
      <path d="M5 12 A7 7 0 1 1 8 17.5" />
      <path d="M5 8 V12 H9" />
    </>
  ),
  moon: <path d="M20 13.5 A8.5 8.5 0 1 1 10.5 4 A7 7 0 0 0 20 13.5 Z" />,
};

export function Icon({ name, size = 18, class: cls = "", title, style }: IconProps) {
  const stroke = size <= 14 ? 1.5 : 1.75;
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      class={`icon icon--${name} ${cls}`}
      style={style}
      fill="none"
      stroke="currentColor"
      stroke-width={stroke}
      stroke-linecap="round"
      stroke-linejoin="round"
      role={title ? "img" : "presentation"}
      aria-hidden={title ? undefined : "true"}
      aria-label={title}
      // 1.75px stroke holds at any render size
      vector-effect="non-scaling-stroke"
    >
      {title && <title>{title}</title>}
      {PATHS[name]}
    </svg>
  );
}
