import type { PlanFrontsResponse } from "../types";
import type { ExecTodayResp, PvTodayResp, TimelineResp } from "../../components/home/v3/dayChart";
import { MOCK_PLAN_FRONTS } from "./planFronts";

// Dev-only fixtures for the Home v3 "O dia" card, reachable ONLY via `?mock=1`.
// Same instant as the other Home mocks: 2026-10-07 14:30 local (BST = UTC+1).
// Shapes only; the numbers are invented and never reach production.
const MOCK_NOW_MS = Date.parse("2026-10-07T13:30:00Z");
const CAP_KWH = 10.36;
const SLOT = 30 * 60000;

const localHour = (ms: number) => (((ms + 3600000) % 86400000) + 86400000) % 86400000 / 3600000;
const dayOfMs = (ms: number) => new Date(ms).toISOString().slice(0, 10);
const bell = (h: number, peak: number) => Math.max(0, peak * Math.sin(((h - 6.5) / 11) * Math.PI));
const iso = (ms: number) => new Date(ms).toISOString().replace(".000Z", "Z");

// Planned battery % by local hour (grid charge 04-06 and 13-16, peak discharge 16-19).
const PLAN_SOC = [[0, 40], [4, 30], [6, 55], [10, 70], [13, 62], [16, 100], [19, 55], [22, 34], [24, 45], [28, 90], [32, 100], [39, 96], [43, 50], [46, 35], [48, 45]];
function planSoc(h: number): number {
  for (let i = 1; i < PLAN_SOC.length; i++) {
    const [h1, v1] = PLAN_SOC[i];
    const [h0, v0] = PLAN_SOC[i - 1];
    if (h <= h1) return v0 + ((v1 - v0) * (h - h0)) / (h1 - h0);
  }
  return 45;
}

export function mockPvToday(dates: string[]): PvTodayResp[] {
  return dates.map((d) => {
    const base = Date.parse(`${d}T00:00:00Z`);
    return {
      date: d, now_utc: iso(MOCK_NOW_MS),
      slots: Array.from({ length: 48 }, (_, i) => {
        const t = base + i * SLOT;
        const h = localHour(t + SLOT / 2);
        const tomorrow = t >= Date.parse("2026-10-07T23:00:00Z");   // next local day: cloudier
        const fc = bell(h, tomorrow ? 0.7 : 1.4) * 0.5;
        const elapsed = t + SLOT <= MOCK_NOW_MS;
        return {
          slot_utc: iso(t),
          pv_forecast_kwh: elapsed ? 0 : fc,
          pv_planned_kwh: fc,
          pv_actual_kwh: elapsed ? fc * 0.92 : null,
          load_forecast_kwh: 0.25 + (h > 16 && h < 22 ? 0.3 : 0) + (h > 4 && h < 7 ? 0.25 : 0),
        };
      }),
    };
  });
}

export function mockExecToday(dates: string[]): ExecTodayResp[] {
  return dates.map((d) => {
    const base = Date.parse(`${d}T00:00:00Z`);
    const slots = Array.from({ length: 48 }, (_, i) => base + i * SLOT)
      .filter((t) => t + SLOT <= MOCK_NOW_MS)
      .map((t) => ({
        slot_utc: iso(t),
        consumption_kwh: 0.3 + 0.25 * Math.abs(Math.sin(t / 4e6)),
        soc_percent: Math.round(planSoc(localHour(t + SLOT)) * 0.97),
      }));
    return { date: d, slots };
  });
}

export function mockTimeline(): TimelineResp {
  const start = Date.parse("2026-10-06T23:00:00Z");   // local midnight
  const slots = Array.from({ length: 96 }, (_, i) => start + i * SLOT).map((t) => ({
    slot_time_utc: iso(t).replace("Z", "+00:00"),
    soc_kwh: (planSoc(((t - start) / 3600000) % 48) / 100) * CAP_KWH,
  }));
  const executed = slots.filter((s) => Date.parse(s.slot_time_utc) + SLOT <= MOCK_NOW_MS);
  const planned = slots.filter((s) => Date.parse(s.slot_time_utc) >= MOCK_NOW_MS);
  const ongoing = slots.find((s) => Date.parse(s.slot_time_utc) <= MOCK_NOW_MS && Date.parse(s.slot_time_utc) + SLOT > MOCK_NOW_MS) ?? null;
  return { ok: true, executed, ongoing, planned };
}

/** Today's fronts mock shifted +24 h (the Cosy bands repeat daily). */
export function mockFrontsTomorrow(): PlanFrontsResponse {
  const shift = (v: unknown): unknown => {
    if (typeof v === "string" && /^\d{4}-\d\d-\d\dT.*Z$/.test(v)) return iso(Date.parse(v) + 86400000);
    if (Array.isArray(v)) return v.map(shift);
    if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, shift(x)]));
    return v;
  };
  const out = shift(MOCK_PLAN_FRONTS) as PlanFrontsResponse;
  return { ...out, date: dayOfMs(MOCK_NOW_MS + 86400000) };
}
