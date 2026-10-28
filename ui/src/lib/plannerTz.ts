import { signal } from "@preact/signals";

/** House timezone (cockpit/now.planner_tz); the nav clock follows it, not the browser. */
export const plannerTz = signal<string | undefined>(undefined);
