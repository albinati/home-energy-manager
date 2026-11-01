import type { DhwBoostStatus } from "../endpoints";

// Dev-only fixture for GET /api/v1/dhw/boost (`?mock=1`). Mutable so the admin
// buttons can be exercised without a backend.
const base: DhwBoostStatus = {
  active: false, row_id: null, started_utc: null, until_utc: null, boost_target_c: null,
  available: true, blocked: null, target_c: 50, hold_hours: 4, uses_powerful: true, reheat_differential_c: 6,
  legionella: { start_utc: "2026-10-11T11:00:00Z", end_utc: "2026-10-11T13:00:00Z" },
};
let state: DhwBoostStatus = { ...base };

export const mockDhwBoost = (): DhwBoostStatus => ({ ...state });
export function mockDhwBoostSet(active: boolean): void {
  state = active
    ? { ...base, active: true, available: false, row_id: 1, boost_target_c: 50,
        started_utc: "2026-10-07T13:30:00Z", until_utc: "2026-10-07T17:30:00Z" }
    : { ...base };
}
