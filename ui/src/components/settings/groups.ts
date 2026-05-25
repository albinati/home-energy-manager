// Settings groups — hand-curated map keyed on visual sections.
// OPTIMIZATION_PRESET is rendered separately as the ModeSwitcher above the
// group list, so it does NOT appear here.

export interface GroupSpec {
  id: string;
  title: string;
  subtitle: string;
  expanded: boolean;
  advanced: boolean;
  keys: string[];
}

export const SETTINGS_GROUPS: GroupSpec[] = [
  {
    id: "dhw-comfort",
    title: "Hot water — comfort",
    subtitle: "Tank targets the LP uses to plan heating. These are the ones you'll change most often.",
    expanded: true,
    advanced: false,
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
    id: "comfort",
    title: "Indoor comfort & control",
    subtitle: "Room setpoint and whether HEM writes to Daikin (passive = read-only, active = HEM drives setpoints).",
    expanded: false,
    advanced: false,
    keys: ["INDOOR_SETPOINT_C", "DAIKIN_CONTROL_MODE"],
  },
  {
    id: "dhw-demand",
    title: "Shower demand model",
    subtitle: "How many showers, how warm, how much flow — drives the planned tank energy each day.",
    expanded: false,
    advanced: false,
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
    title: "Legionella cycle (prediction)",
    subtitle: "Daikin Onecta firmware runs the weekly thermal-shock autonomously — these values are used only to model the expected load. Must match the firmware schedule for accurate forecasts.",
    expanded: false,
    advanced: true,
    keys: [
      "DHW_LEGIONELLA_DAY",
      "DHW_LEGIONELLA_HOUR_LOCAL",
      "DHW_LEGIONELLA_DURATION_MIN",
      "DHW_LEGIONELLA_TANK_TARGET_C",
    ],
  },
  {
    id: "schedule",
    title: "Schedule & cron",
    subtitle: "When the LP, MPC, and telemetry jobs fire. Changes hot-reload APScheduler — no restart needed.",
    expanded: false,
    advanced: true,
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
    subtitle: "How much the LP values battery state at the end of the 48 h horizon. Higher value → solver hoards charge for tomorrow.",
    expanded: false,
    advanced: true,
    keys: ["LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", "LP_SOC_FINAL_KWH"],
  },
  {
    id: "calibration",
    title: "Calibration & location",
    subtitle: "PV calibration rolling window + site coordinates for Open-Meteo / Quartz forecasts.",
    expanded: false,
    advanced: true,
    keys: ["PV_CALIBRATION_WINDOW_DAYS", "WEATHER_LAT", "WEATHER_LON"],
  },
  {
    id: "safety",
    title: "Safety gates",
    subtitle: "Lock-down switches for batch settings writes.",
    expanded: false,
    advanced: true,
    keys: ["REQUIRE_SIMULATION_ID"],
  },
];

// Friendly labels — keep short; full descriptions come from the backend `description` field.
export const KEY_LABELS: Record<string, string> = {
  OPTIMIZATION_PRESET: "Household mode",
  DHW_TEMP_NORMAL_C: "Normal tank target",
  DHW_TEMP_COMFORT_C: "Comfort tank target (plunge fill)",
  DHW_TEMP_PV_ABUNDANCE_TARGET_C: "PV-abundance tank target",
  DHW_TANK_OVERNIGHT_TARGET_C: "Overnight setback target",
  DHW_MORNING_RESERVE_HOUR_LOCAL: "Morning reserve hour",
  DHW_TANK_USABLE_FRACTION: "Tank usable fraction",
  DHW_SHOWER_DURATION_MIN: "Shower duration",
  DHW_SHOWER_FLOW_LPM: "Shower flow rate",
  DHW_SHOWER_MIXER_TEMP_C: "Mixer outlet temperature",
  DHW_SHOWER_COLD_INLET_TEMP_C: "Cold inlet temperature",
  DHW_SHOWERS_NORMAL_EVENING: "Evening showers (normal mode)",
  DHW_SHOWERS_NORMAL_MORNING_RESERVE: "Morning shower reserve (normal mode)",
  DHW_SHOWERS_EVENING_CAP: "Evening shower cap",
  DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST: "Guest extra evening showers (per guest)",
  DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST: "Guest extra morning showers (per guest)",
  DHW_GUEST_COUNT: "Guest count (guests mode)",
  DHW_LEGIONELLA_DAY: "Legionella weekday",
  DHW_LEGIONELLA_HOUR_LOCAL: "Legionella start hour",
  DHW_LEGIONELLA_DURATION_MIN: "Legionella duration",
  DHW_LEGIONELLA_TANK_TARGET_C: "Legionella tank target",
  INDOOR_SETPOINT_C: "Indoor setpoint",
  DAIKIN_CONTROL_MODE: "Daikin control mode",
  REQUIRE_SIMULATION_ID: "Require X-Simulation-Id for batch writes",
  LP_PLAN_PUSH_HOUR: "Plan-push hour (UTC)",
  LP_PLAN_PUSH_MINUTE: "Plan-push minute (UTC)",
  MPC_FORECAST_REFRESH_INTERVAL_MINUTES: "MPC forecast refresh",
  PV_TELEMETRY_INTERVAL_MINUTES: "PV telemetry interval",
  PV_CALIBRATION_WINDOW_DAYS: "PV calibration window",
  WEATHER_LAT: "Site latitude",
  WEATHER_LON: "Site longitude",
  LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH: "Terminal SoC value",
  LP_SOC_FINAL_KWH: "Terminal SoC target",
};

export function labelFor(key: string): string {
  return KEY_LABELS[key] || key;
}

export function unitFor(key: string): string {
  if (key.endsWith("_C")) return "°C";
  if (key.endsWith("_KWH")) return "kWh";
  if (key.endsWith("_MIN") || key.endsWith("_MINUTES")) return "min";
  if (key.endsWith("_MINUTE")) return "";
  if (key.endsWith("_HOUR") || key.endsWith("_HOUR_LOCAL")) return "h";
  if (key.endsWith("_LPM")) return "L/min";
  if (key.endsWith("_PENCE_PER_KWH")) return "p/kWh";
  if (key.endsWith("_DAYS")) return "d";
  if (key.endsWith("_DAY")) return "";
  if (key.endsWith("_FRACTION") || key.endsWith("_PERCENT")) return "";
  return "";
}
