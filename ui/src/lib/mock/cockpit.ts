import type { CockpitNow } from "../types";

// Dev-only fixture for GET /api/v1/cockpit/now, reachable ONLY via `?mock=1`
// (see landing.tsx). Same instant as the plan/fronts mock: 14:30 local (BST),
// cheap band, battery charging from the grid at 1.2 kW.
export const MOCK_COCKPIT: CockpitNow = {
  now_utc: "2026-10-07T13:30:00Z",
  planner_tz: "Europe/London",
  state: {
    soc_pct: 64, soc_kwh: 5.8, solar_kw: 0.3, load_kw: 0.4, grid_kw: 1.3, battery_kw: 1.2,
    tank_c: 46, indoor_c: 21.2, lwt_c: 30, daikin_mode: "heating",
  },
  current_slot: { price_import_p: 12.49, price_export_p: 15, fox_mode: "ForceCharge", kind: "cheap" },
  next_transition: null,
  freshness: { fox: { age_s: 20, fetched_at_utc: "2026-10-07T13:29:40Z", stale: false } },
};
