// Insights recommendations (#879 PR 2) — GET /api/v1/insights/recommendations.
// Types only (no values) so the pure helpers can import them under node's type stripping.

export type RecKind = "status" | "suggestion" | "setting";
export interface RecAction { setting_key: string; from: unknown; to: unknown }
export interface RecItem {
  key: string;
  who: string;
  icon: string;
  headline: string;
  detail: string;
  money_pence: number | null;
  money_label: string | null;
  kind: RecKind;
  action: RecAction | null;
  evidence?: Record<string, unknown>;
  /** #876 s2: auto-tune ledger state of this suggestion's key (last 14 days). */
  ledger?: { status: "evaluating" | "kept" | "rolled_back"; applied_at: string; day_n: number; id: number; intent?: string | null };
}
export interface RecBandShares { cheap: number | null; day: number | null; peak: number | null; solar: number | null }
export interface RecWeekSummary {
  /** Complete days of the range the figures cover (today's partial day is left out). */
  complete_days: number;
  net_gbp: number | null;
  usual_gbp: number | null;
  current_label: string | null;
  compare: Array<{ short_label: string; delta_gbp: number; approximate?: boolean }>;
  energy: null | {
    total_kwh: number | null;
    by_band: RecBandShares;
    shares: RecBandShares;
    peak_kwh: number | null;
    formula?: string;
  };
  solar: null | {
    kwh: number | null; self_use_share: number | null; export_kwh: number | null;
    export_gbp: number | null; forecast_mape: number | null;
  };
  battery: null | {
    arbitrage_gbp: number | null; cheap_bought_kwh: number | null; cheap_p: number | null;
    cycles_per_day: number | null; peak_covered_days: number; days: number; formula?: string;
  };
}
export interface RecHouse {
  cooling_c_per_h?: { value: number; outdoor_c: number; tau_h: number };
  heating_c_per_h?: { value: number; offset_c: number; lag_min: number | null };
  solar_gain?: {
    coefficient: number; value_c_per_sunny_afternoon: number | null; applied: boolean;
    assumed_pv_kw?: number; assumed_hours?: number;
  };
  tank?: { c_per_h_night: number; ratio_median_recent: number | null; n_recent?: number | null };
}
export interface RecommendationsResponse {
  generated_at: string;
  week: { start: string; end: string; complete_days?: number; last_complete?: string | null };
  items: RecItem[];
  week_summary: RecWeekSummary;
  house: RecHouse;
}
