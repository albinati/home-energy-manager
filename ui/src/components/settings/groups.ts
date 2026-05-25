// Settings groups — hand-curated map keyed on the 8 visual sections.
// OPTIMIZATION_PRESET is rendered separately as the ModeSwitcher above the
// group list, so it does NOT appear here.

export interface GroupSpec {
  id: string;
  title: string;
  subtitle: string;
  expanded: boolean;
  keys: string[];
}

export const SETTINGS_GROUPS: GroupSpec[] = [
  {
    id: "dhw-comfort",
    title: "Hot-water comfort",
    subtitle: "Tank targets the LP solver uses to plan heating",
    expanded: true,
    keys: [
      "DHW_TEMP_NORMAL_C",
      "DHW_TEMP_COMFORT_C",
      "DHW_TEMP_PV_ABUNDANCE_TARGET_C",
      "DHW_TANK_OVERNIGHT_TARGET_C",
      "DHW_MORNING_RESERVE_HOUR_LOCAL",
      "DHW_TANK_USABLE_FRACTION",
    ],
  },
  {
    id: "dhw-demand",
    title: "Shower demand model",
    subtitle: "How many showers, how warm, how much flow — drives planned tank energy",
    expanded: false,
    keys: [
      "DHW_SHOWERS_NORMAL_EVENING",
      "DHW_SHOWERS_NORMAL_MORNING_RESERVE",
      "DHW_SHOWERS_EVENING_CAP",
      "DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST",
      "DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST",
      "DHW_GUEST_COUNT",
      "DHW_SHOWER_DURATION_MIN",
      "DHW_SHOWER_FLOW_LPM",
      "DHW_SHOWER_MIXER_TEMP_C",
      "DHW_SHOWER_COLD_INLET_TEMP_C",
    ],
  },
  {
    id: "legionella",
    title: "Legionella cycle",
    subtitle: "Must match the Daikin Onecta firmware schedule (HEM only models, does not drive it)",
    expanded: false,
    keys: [
      "DHW_LEGIONELLA_DAY",
      "DHW_LEGIONELLA_HOUR_LOCAL",
      "DHW_LEGIONELLA_DURATION_MIN",
      "DHW_LEGIONELLA_TANK_TARGET_C",
    ],
  },
  {
    id: "comfort",
    title: "Indoor comfort & control",
    subtitle: "Room setpoint and whether HEM writes to Daikin",
    expanded: false,
    keys: ["INDOOR_SETPOINT_C", "DAIKIN_CONTROL_MODE"],
  },
  {
    id: "schedule",
    title: "Schedule & cron",
    subtitle: "When the LP, MPC, and telemetry jobs fire — changes hot-reload the scheduler",
    expanded: false,
    keys: [
      "LP_PLAN_PUSH_HOUR",
      "LP_PLAN_PUSH_MINUTE",
      "MPC_FORECAST_REFRESH_INTERVAL_MINUTES",
      "PV_TELEMETRY_INTERVAL_MINUTES",
    ],
  },
  {
    id: "terminal-soc",
    title: "Terminal SoC valuation",
    subtitle: "How much the LP values battery state at the end of the planning horizon",
    expanded: false,
    keys: ["LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", "LP_SOC_FINAL_KWH"],
  },
  {
    id: "calibration",
    title: "Calibration & location",
    subtitle: "PV calibration window + site coordinates for Open-Meteo / Quartz",
    expanded: false,
    keys: ["PV_CALIBRATION_WINDOW_DAYS", "WEATHER_LAT", "WEATHER_LON"],
  },
  {
    id: "safety",
    title: "Safety gates",
    subtitle: "Lock-down switches for batch settings writes",
    expanded: false,
    keys: ["REQUIRE_SIMULATION_ID"],
  },
];

// Friendly labels — keep short; full descriptions come from the backend `description` field.
export const KEY_LABELS: Record<string, string> = {
  OPTIMIZATION_PRESET: "Household mode",
  DHW_TEMP_NORMAL_C: "Normal tank target",
  DHW_TEMP_COMFORT_C: "Comfort tank target (negative-price plunge)",
  DHW_TEMP_PV_ABUNDANCE_TARGET_C: "PV-abundance tank target",
  DHW_TANK_OVERNIGHT_TARGET_C: "Overnight tank target",
  DHW_MORNING_RESERVE_HOUR_LOCAL: "Morning reserve hour (local)",
  DHW_TANK_USABLE_FRACTION: "Tank usable fraction",
  DHW_SHOWER_DURATION_MIN: "Shower duration (min)",
  DHW_SHOWER_FLOW_LPM: "Shower flow rate (L/min)",
  DHW_SHOWER_MIXER_TEMP_C: "Mixer outlet temperature",
  DHW_SHOWER_COLD_INLET_TEMP_C: "Cold inlet temperature",
  DHW_SHOWERS_NORMAL_EVENING: "Evening showers (normal mode)",
  DHW_SHOWERS_NORMAL_MORNING_RESERVE: "Morning shower reserve (normal mode)",
  DHW_SHOWERS_EVENING_CAP: "Evening shower cap",
  DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST: "Guest extra showers (evening, per guest)",
  DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST: "Guest extra showers (morning, per guest)",
  DHW_GUEST_COUNT: "Guest count (guests mode)",
  DHW_LEGIONELLA_DAY: "Legionella weekday (-1 disabled, 0=Mon)",
  DHW_LEGIONELLA_HOUR_LOCAL: "Legionella hour (local)",
  DHW_LEGIONELLA_DURATION_MIN: "Legionella duration (min)",
  DHW_LEGIONELLA_TANK_TARGET_C: "Legionella tank target",
  INDOOR_SETPOINT_C: "Indoor setpoint",
  DAIKIN_CONTROL_MODE: "Daikin control mode",
  REQUIRE_SIMULATION_ID: "Require X-Simulation-Id for batch writes",
  LP_PLAN_PUSH_HOUR: "Plan-push hour (UTC)",
  LP_PLAN_PUSH_MINUTE: "Plan-push minute (UTC)",
  MPC_FORECAST_REFRESH_INTERVAL_MINUTES: "MPC forecast refresh (min)",
  PV_TELEMETRY_INTERVAL_MINUTES: "PV telemetry interval (min)",
  PV_CALIBRATION_WINDOW_DAYS: "PV calibration window (days)",
  WEATHER_LAT: "Site latitude",
  WEATHER_LON: "Site longitude",
  LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH: "Terminal SoC value (p/kWh)",
  LP_SOC_FINAL_KWH: "Terminal SoC target (kWh)",
};

export function labelFor(key: string): string {
  return KEY_LABELS[key] || key;
}
