import type { JSX } from "preact";

// Inline icons for the "O que fazer" rows. 24x24, 1.75 stroke, currentColor only
// (same family as common/Icon). Unknown names fall back to the house.
const PATHS: Record<string, JSX.Element> = {
  washer: (<><rect x="4" y="3" width="16" height="18" rx="2" /><circle cx="12" cy="13" r="4.2" /><path d="M7.5 6.5h.01M10.5 6.5h.01" /></>),
  breakfast: (<><path d="M5 9h11v5a5 5 0 0 1-5 5h-1a5 5 0 0 1-5-5z" /><path d="M16 10h2a2 2 0 0 1 0 4h-2" /><path d="M8 3v3M12 3v3" /></>),
  lunch: (<><circle cx="10" cy="14" r="6" /><path d="M16 14h5" /><path d="M7 3.5c0 1.2 1.2 1.2 1.2 2.4M11 3.5c0 1.2 1.2 1.2 1.2 2.4" /></>),
  dinner: (<><circle cx="12" cy="12" r="8.5" /><path d="M12 7v5l3 2" /></>),
  drying: (<><circle cx="12" cy="12" r="4" /><path d="M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M18.7 5.3l-1.8 1.8M7.1 16.9l-1.8 1.8" /></>),
  drop: (<path d="M12 3.2c3.4 4 6 7 6 10.2a6 6 0 0 1-12 0C6 10.200 8.600 7.200 12 3.200z" />),
  thermo: (<><path d="M10 14.5V5a2 2 0 0 1 4 0v9.500a4 4 0 1 1-4 0z" /><path d="M12 9v7" /></>),
  house: (<><path d="M3.5 11 12 4l8.500 7" /><path d="M5.500 9.500V20h13V9.500" /><path d="M10 20v-5h4v5" /></>),
};

export function RecIcon({ name, size = 22 }: { name: string; size?: number }) {
  return (
    <svg class="is-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      {PATHS[name] ?? PATHS.house}
    </svg>
  );
}
