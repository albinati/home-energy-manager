import type { PlanFrontsResponse, PlanWindowStatus, ConsumptionBand } from "../types";

// Dev-only fixture for GET /api/v1/plan/fronts, reachable ONLY via `?mock=1`
// (see landing.tsx). A realistic Cosy Tuesday, "now" = 14:30 local (BST).
const D = "2026-10-07";
const utc = (hhmm: string) => {
  const [h, m] = hhmm.split(":").map(Number);
  const t = new Date(Date.UTC(2026, 9, 7, h - 1, m)); // BST = UTC+1
  return t.toISOString().replace(".000Z", "Z");
};
const win = (s: string, e: string) => ({
  start_utc: utc(s), end_utc: e === "24:00" ? "2026-10-07T23:00:00Z" : utc(e), start_local: s, end_local: e,
});

const band = (
  key: string, label: string, s: string, e: string, price_p: number, status: PlanWindowStatus,
  progress: number, hours: number, exp: [number, number, number], committed: number | null,
  realised: number | null, under: number,
): ConsumptionBand => ({
  key, label, ...win(s, e), hours, price_p, status, progress,
  expected_kwh: { p50: exp[0], p75: exp[1], p90: exp[2], max: exp[2] * 1.15, n_days: 22 },
  committed_kwh: committed, realised_kwh: realised,
  forecast_error_kwh: { mean: 0.18, p90: 0.62, under_forecast_days: under, n_days: 22 },
});

export const MOCK_PLAN_FRONTS: PlanFrontsResponse = {
  date: D,
  now_utc: utc("14:30"),
  tariff: {
    display_name: "Cosy", structure: "banded",
    windows: [
      { key: "band_cheap", label: "cheap", ...win("04:00", "07:00"), price_p: 12.49, status: "done" },
      { key: "band_day", label: "day", ...win("07:00", "13:00"), price_p: 25.45, status: "done" },
      { key: "band_cheap", label: "cheap", ...win("13:00", "16:00"), price_p: 12.49, status: "ongoing" },
      { key: "band_peak", label: "peak", ...win("16:00", "19:00"), price_p: 38.17, status: "upcoming" },
      { key: "band_day", label: "day", ...win("19:00", "22:00"), price_p: 25.45, status: "upcoming" },
      { key: "band_cheap", label: "cheap", ...win("22:00", "24:00"), price_p: 12.49, status: "upcoming" },
    ],
  },
  battery: {
    capacity_kwh: 10.36, reserve_pct: 15, soc_now_pct: 62, soc_now_kwh: 6.4,
    plan_run_id: 4882, plan_run_at: utc("14:05"),
    windows: [
      { kind: "grid_charge", ...win("04:00", "06:00"), grid_kwh: 1.8, charge_kwh: 1.7, discharge_kwh: 0, soc_start_pct: 22, soc_end_pct: 38, fox_mode: "ForceCharge" },
      { kind: "pv_charge", ...win("09:00", "13:00"), grid_kwh: 0, charge_kwh: 3.1, discharge_kwh: 0, soc_start_pct: 38, soc_end_pct: 68, fox_mode: "SelfUse" },
      { kind: "grid_charge", ...win("13:30", "16:00"), grid_kwh: 2.4, charge_kwh: 2.3, discharge_kwh: 0, soc_start_pct: 62, soc_end_pct: 94, fox_mode: "ForceCharge" },
      { kind: "self_use", ...win("16:00", "19:00"), grid_kwh: 0, charge_kwh: 0, discharge_kwh: 4.6, soc_start_pct: 94, soc_end_pct: 50, fox_mode: "SelfUse" },
      { kind: "hold", ...win("22:00", "24:00"), grid_kwh: 0.4, charge_kwh: 0.3, discharge_kwh: 0, soc_start_pct: 31, soc_end_pct: 34, fox_mode: "Backup" },
    ],
    by_band: [
      { key: "band_cheap", label: "cheap", start_local: "13:00", end_local: "16:00", planned_import_kwh: 2.4, realised_import_kwh: 0.6, soc_entry_pct: 62, floored: false },
      { key: "band_peak", label: "peak", start_local: "16:00", end_local: "19:00", planned_import_kwh: 0, realised_import_kwh: null, soc_entry_pct: 94, floored: true },
      { key: "band_day", label: "day", start_local: "19:00", end_local: "22:00", planned_import_kwh: 1.1, realised_import_kwh: null, soc_entry_pct: 50, floored: false },
      { key: "band_cheap", label: "cheap", start_local: "22:00", end_local: "24:00", planned_import_kwh: 0.4, realised_import_kwh: null, soc_entry_pct: 31, floored: false },
    ],
    fox_groups: [
      { mode: "ForceCharge", start_local: "13:30", end_local: "15:59", min_soc: 10, fd_soc: 10, fd_pwr: 0, max_soc: 94 },
      { mode: "SelfUse", start_local: "16:00", end_local: "21:59", min_soc: 15, fd_soc: 10, fd_pwr: 0, max_soc: 100 },
      { mode: "Backup", start_local: "22:00", end_local: "23:59", min_soc: 15, fd_soc: 10, fd_pwr: 0, max_soc: 100 },
    ],
    peak_import_planned_kwh: 0, peak_import_realised_kwh: null,
  },
  tank: {
    tank_now_c: 42.8, target_now_c: 45, power_on: true, telemetry_at_utc: utc("14:28"),
    model: { source: "measured_indoor", ua_w_per_k: 3.2, ambient_c: 21.4, tau_hours: 62, coast_measured_c_per_h: 0.37, coast_model_c_per_h: 0.3, coast_ratio: 1.23 },
    decision: { arm: "boost", warmup_hour: 13, setback_hour: 16, warmup_target_c: 47, peak_entry_hour: 16, cost_hold_p: 41.2, cost_boost_p: 36.8 },
    windows: [
      { kind: "warmup", ...win("13:00", "16:00"), tank_target_c: 47 },
      { kind: "setback", ...win("16:00", "24:00"), tank_target_c: 37 },
    ],
    showers: [
      { start_local: "20:00", end_local: "22:00", floor_c: 45, label: "evening showers", predicted_tank_c: 44.6 },
      { start_local: "06:30", end_local: "08:00", floor_c: 43, label: "morning showers", predicted_tank_c: 44.1 },
    ],
    next_action: { kind: "setback", start_local: "16:00", tank_target_c: 37 },
  },
  heating: {
    indoor_now_c: 21.2, indoor_rooms_c: { corredor: 21.6, sala: 21.0, quarto: 20.9 }, indoor_aggregate: "mean", outdoor_now_c: 12.4,
    setpoint_c: 21, night_floor_c: 17.5, peak_coast_delta_c: 1, lwt_source: "tier",
    gate: { preheat_enabled: true, demand_present: true, measured_window_kwh: 1.8, threshold_kwh: 0.5, current_outdoor_c: 12.4, outdoor_cutoff_c: 16, positive_offset_suppressed_by_outdoor: false, preheat_suppressed: false, lp_available: true },
    windows: [
      { kind: "boost", offset_c: 3, ...win("04:00", "07:00"), source: "tier" },
      { kind: "boost", offset_c: 3, ...win("13:00", "16:00"), source: "tier" },
      { kind: "setback", offset_c: -2, ...win("16:00", "19:00"), source: "tier" },
      { kind: "restore", offset_c: 0, ...win("19:00", "22:00"), source: "tier" },
    ],
    predicted_indoor: { min_c: 19.8, max_c: 21.9, at_07_c: 20.6, at_16_c: 21.7, at_19_c: 20.4, at_22_c: 20.9 },
    by_band: [
      { key: "band_cheap", label: "cheap", start_local: "13:00", end_local: "16:00", indoor_min_c: 21.2, indoor_max_c: 21.9, offset_mode: "boost" },
      { key: "band_peak", label: "peak", start_local: "16:00", end_local: "19:00", indoor_min_c: 20.4, indoor_max_c: 21.7, offset_mode: "setback" },
      { key: "band_day", label: "day", start_local: "19:00", end_local: "22:00", indoor_min_c: 20.4, indoor_max_c: 20.9, offset_mode: "neutral" },
    ],
  },
  consumption: {
    date: D, now_utc: utc("14:30"), tariff_display_name: "Cosy", tariff_structure: "banded", day_type: "weekday", history_days: 22,
    bands: [
      band("band_cheap", "cheap", "04:00", "07:00", 12.49, "done", 1, 3, [1.4, 1.8, 2.3], 1.5, 1.6, 9),
      band("band_day", "day", "07:00", "13:00", 25.45, "done", 1, 6, [3.2, 3.9, 4.8], 3.4, 3.1, 11),
      band("band_cheap", "cheap", "13:00", "16:00", 12.49, "ongoing", 0.5, 3, [2.0, 2.6, 3.3], 2.3, 0.9, 10),
      band("band_peak", "peak", "16:00", "19:00", 38.17, "upcoming", 0, 3, [3.1, 3.9, 4.9], 3.2, null, 18),
      band("band_day", "day", "19:00", "22:00", 25.45, "upcoming", 0, 3, [2.6, 3.2, 3.9], 2.8, null, 8),
      band("band_cheap", "cheap", "22:00", "24:00", 12.49, "upcoming", 0, 2, [1.0, 1.3, 1.7], 1.1, null, 7),
    ],
    day: { expected_kwh: { p50: 14.2, p75: 16.8, p90: 19.9, n_days: 22 }, committed_kwh: 14.3, realised_kwh: 5.6 },
  },
  spend: {
    realised_import_kwh: 5.2, realised_import_cost_gbp: 0.86, realised_avg_import_p: 16.5,
    forecast_import_kwh: 8.9, forecast_avg_import_p: 17.2, peak_import_kwh: 0,
    ideal_avg_import_p: 12.49, score: "below",
    score_thresholds: { ideal_max_p: 14.36, above_min_p: 18.97 }, score_basis: "realised",
    period: {
      week: { n_days: 2, net_cost_gbp: 3.1, per_day_gbp: 1.55, import_kwh: 17.4, avg_import_p: 17.8 },
      month: { n_days: 7, net_cost_gbp: 11.2, per_day_gbp: 1.6, import_kwh: 61.2, avg_import_p: 18.3 },
    },
  },
  compare: {
    period: "month", period_start: "2026-10-01", period_end: "2026-10-07", n_days: 7,
    current: { product_code: "COSY-22-12-08", display_name: "Cosy", net_gbp: 11.2 },
    rows: [
      { product_code: "COSY-22-12-08", display_name: "Cosy (current)", net_gbp: 11.2, approximate: false, is_current: true, delta_vs_current_gbp: 0 },
      { product_code: "BG-FIXED", display_name: "British Gas Fixed", net_gbp: 14.9, approximate: false, is_current: false, delta_vs_current_gbp: 3.7 },
      { product_code: "SVT", display_name: "SVT", net_gbp: 15.6, approximate: false, is_current: false, delta_vs_current_gbp: 4.4 },
      { product_code: "AGILE", display_name: "Agile", net_gbp: 10.4, approximate: true, is_current: false, delta_vs_current_gbp: -0.8 },
    ],
    framing: "Staying on Cosy for the contract — comparison is informational",
  },
};
