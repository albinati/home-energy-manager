// Settings layout (#879 PR 3).
//
// Two visible groups (Conforto, Agua quente) + a small Conta group, then
// Avancado: EVERY other runtime key of src/runtime_settings.py, grouped and
// searchable. The household mode (OPTIMIZATION_PRESET) is controlled on the
// Home (ModeBox); Settings shows it read-only. It stays listed under Avancado
// as a plain select only because the backend-only "guests" value is reachable
// nowhere else.
//
// Pure module (no preact imports): unit-tested with `npm test` and checked
// against the backend registry by tests/test_ui_settings_groups.py.

export interface GroupSpec {
  id: string;
  title: string;
  subtitle: string;
  keys: string[];
}

export interface PlainCopy {
  label: string;
  hint: string;
}

export const VISIBLE_GROUPS: GroupSpec[] = [
  {
    id: "conforto",
    title: "Conforto",
    subtitle: "Como a casa deve ficar. O sistema planeja o aquecimento para respeitar estes limites.",
    keys: [
      "INDOOR_SETPOINT_C",
      "LP_W3_NIGHT_FLOOR_C",
      "LP_W3_PEAK_COAST_DELTA_C",
      "LP_W3_CEILING_C",
      "INDOOR_COMFORT_AGGREGATE",
    ],
  },
  {
    id: "agua-quente",
    title: "Água quente",
    subtitle: "Temperatura do tanque e horários de banho.",
    keys: [
      "DHW_TEMP_NORMAL_C",
      "DHW_TEMP_COMFORT_C",
      "DHW_SHOWER_EVENING_START_HOUR",
      "DHW_SHOWER_EVENING_END_HOUR",
      "DHW_MORNING_RESERVE_START_HOUR",
      "DHW_MORNING_RESERVE_END_HOUR",
    ],
  },
  {
    id: "conta",
    title: "Conta",
    subtitle: "",
    keys: ["OCTOPUS_BILLING_CYCLE_DAY"],
  },
];

export const ADVANCED_GROUPS: GroupSpec[] = [
  {
    id: "lwt",
    title: "Aquecimento (LWT)",
    subtitle: "Como o plano chega à bomba de calor Daikin: fonte dos ajustes, modo de descanso e modelo térmico.",
    keys: [
      "DAIKIN_LWT_SOURCE",
      "DAIKIN_LWT_COAST_MODE",
      "LP_W3_EMERGENCY_FLOOR_C",
      "LP_COP_LWT_OFFSET_PENALTY_PER_K",
      "LP_W3_EMITTER_TAU_MIN",
      "LP_W3_INTERNAL_GAIN_KW",
      "LP_W3_SOLAR_GAIN_KW_PER_PV_KW",
    ],
  },
  {
    id: "carga-bateria",
    title: "Carga e bateria",
    subtitle: "Previsão de consumo e regras de uso da bateria.",
    keys: [
      "LP_LOAD_SCALE_FACTOR",
      "LP_LOAD_EXPENSIVE_BAND_QUANTILE",
      "LP_GUESTS_BASE_LOAD_SCALE",
      "LP_VACATION_LOAD_SCALE",
      "LP_POSITIVE_HOLD_ENABLED",
      "LP_POSITIVE_HOLD_MIN_UPLIFT_PENCE",
      "LP_POSITIVE_HOLD_MAX_GROUPS",
      "LP_POSITIVE_HOLD_MIN_SOC_MARGIN_PCT",
      "LP_SOLAR_CHARGE_FOX_MODE",
    ],
  },
  {
    id: "ferias-daikin",
    title: "Férias e Daikin",
    subtitle: "Volta de férias, modo férias da Daikin e se o HEM escreve na bomba. O modo da casa se muda na Casa; o seletor aqui existe só para o modo hóspedes.",
    keys: [
      "OPTIMIZATION_PRESET",
      "VACATION_RETURN_AT",
      "VACATION_RECOVERY_HOURS",
      "VACATION_DAIKIN_HOLIDAY_ENABLED",
      "DAIKIN_HOLIDAY_DETECT_ENABLED",
      "DAIKIN_CONTROL_MODE",
    ],
  },
  {
    id: "dhw-model",
    title: "Modelo da água quente",
    subtitle: "Metas do tanque, janelas de aquecimento e a janela dinâmica.",
    keys: [
      "DHW_TEMP_PV_ABUNDANCE_TARGET_C",
      "DHW_TANK_OVERNIGHT_TARGET_C",
      "DHW_SHOWER_COMFORT_C",
      "DHW_MORNING_RESERVE_C",
      "DHW_MORNING_RESERVE_HOUR_LOCAL",
      "DHW_TANK_USABLE_FRACTION",
      "DHW_WARMUP_START_HOUR_LOCAL",
      "DHW_SETBACK_START_HOUR_LOCAL",
      "DHW_WARMUP_PRICE_AWARE_ENABLED",
      "DHW_WARMUP_WINDOW_START_LOCAL",
      "DHW_WARMUP_WINDOW_END_LOCAL",
      "DHW_DYNAMIC_WINDOW_ENABLED",
      "DHW_DYNAMIC_BOOST_HOLD_HOURS",
    ],
  },
  {
    id: "dhw-demand",
    title: "Demanda de banho",
    subtitle: "Quantos banhos, quão quentes e com que vazão: define a energia planejada para o tanque.",
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
    title: "Legionella (previsão)",
    subtitle: "O ciclo semanal roda sozinho no firmware da Daikin; estes valores só alimentam a previsão e devem coincidir com a unidade.",
    keys: [
      "DHW_LEGIONELLA_DAY",
      "DHW_LEGIONELLA_HOUR_LOCAL",
      "DHW_LEGIONELLA_DURATION_MIN",
      "DHW_LEGIONELLA_TANK_TARGET_C",
    ],
  },
  {
    id: "schedule",
    title: "Horários e agendador",
    subtitle: "Quando rodam o plano e a telemetria. Mudanças recarregam o agendador sem reiniciar.",
    keys: [
      "LP_PLAN_PUSH_HOUR",
      "LP_PLAN_PUSH_MINUTE",
      "MPC_FORECAST_REFRESH_INTERVAL_MINUTES",
      "PV_TELEMETRY_INTERVAL_MINUTES",
    ],
  },
  {
    id: "terminal-soc",
    title: "Bateria no fim do plano",
    subtitle: "Quanto vale a carga que sobra ao fim das 48 h. Valor maior = o plano guarda mais carga para amanhã.",
    keys: ["LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", "LP_SOC_FINAL_KWH"],
  },
  {
    id: "calibration",
    title: "Calibração e local",
    subtitle: "Janela de calibração do solar e coordenadas para a previsão do tempo.",
    keys: ["PV_CALIBRATION_WINDOW_DAYS", "WEATHER_LAT", "WEATHER_LON"],
  },
  {
    id: "safety",
    title: "Travas de segurança",
    subtitle: "Chaves que travam a escrita de ajustes em lote.",
    keys: ["REQUIRE_SIMULATION_ID"],
  },
];

// Plain-words copy for the keys in the visible groups (replaces the backend
// description, which is written for developers).
export const PLAIN_COPY: Record<string, PlainCopy> = {
  INDOOR_SETPOINT_C: { label: "Temperatura da casa de dia", hint: "A temperatura que a casa deve ter durante o dia." },
  LP_W3_NIGHT_FLOOR_C: { label: "Mínimo à noite (22–07)", hint: "A casa pode esfriar até aqui de madrugada." },
  LP_W3_PEAK_COAST_DELTA_C: { label: "Quanto pode esfriar no pico 16–19", hint: "Graus abaixo da temperatura do dia aceitos com o aquecimento desligado no pico. 0 = não esfriar." },
  LP_W3_CEILING_C: { label: "Máximo (não aquecer acima)", hint: "Acima disto o aquecimento não liga, nem com energia barata." },
  INDOOR_COMFORT_AGGREGATE: { label: "Qual sensor é “a casa”", hint: "mean = média dos cômodos, min = o mais frio, max = o mais quente, ou room:nome para um cômodo só." },
  DHW_TEMP_NORMAL_C: { label: "Temperatura normal do tanque", hint: "Meta do dia a dia, suficiente para os banhos." },
  DHW_TEMP_COMFORT_C: { label: "Temperatura mínima quando aquece", hint: "Quando o sistema decide aquecer a água, ela sobe pelo menos até aqui." },
  DHW_SHOWER_EVENING_START_HOUR: { label: "Banhos da noite: começam às", hint: "Hora do dia (0–24) em que os banhos da noite começam." },
  DHW_SHOWER_EVENING_END_HOUR: { label: "Banhos da noite: terminam às", hint: "Hora do dia (0–24) em que os banhos da noite terminam." },
  DHW_MORNING_RESERVE_START_HOUR: { label: "Banho da manhã: a partir das", hint: "Hora do dia (0–24) em que começa a reserva de água da manhã." },
  DHW_MORNING_RESERVE_END_HOUR: { label: "Banho da manhã: até às", hint: "Hora do dia (0–24) em que acaba a reserva da manhã." },
  OCTOPUS_BILLING_CYCLE_DAY: { label: "Dia do ciclo de fatura da Octopus", hint: "Dia do mês em que começa cada ciclo da conta (1–28)." },
};

// Friendly labels for the Avancado keys; keys without an entry fall back to
// the raw key (still searchable, description comes from the backend).
export const KEY_LABELS: Record<string, string> = {
  DAIKIN_LWT_SOURCE: "Fonte dos ajustes da Daikin (tier | lp)",
  DAIKIN_LWT_COAST_MODE: "Modo de descanso do aquecimento",
  LP_W3_EMERGENCY_FLOOR_C: "Piso de emergência (°C)",
  LP_COP_LWT_OFFSET_PENALTY_PER_K: "Perda de eficiência por grau de reforço",
  LP_W3_EMITTER_TAU_MIN: "Atraso dos radiadores (minutos)",
  LP_W3_INTERNAL_GAIN_KW: "Calor interno da casa (kW)",
  LP_W3_SOLAR_GAIN_KW_PER_PV_KW: "Ganho solar (kW por kW de painel)",
  LP_LOAD_SCALE_FACTOR: "Fator da previsão de consumo",
  LP_LOAD_EXPENSIVE_BAND_QUANTILE: "Folga da previsão nas faixas caras (p75 | p90)",
  LP_GUESTS_BASE_LOAD_SCALE: "Consumo base com hóspedes (fator)",
  LP_VACATION_LOAD_SCALE: "Consumo da casa em férias (fator)",
  LP_POSITIVE_HOLD_ENABLED: "Segurar bateria em preço positivo",
  LP_POSITIVE_HOLD_MIN_UPLIFT_PENCE: "Ganho mínimo para segurar (p)",
  LP_POSITIVE_HOLD_MAX_GROUPS: "Máximo de blocos de retenção",
  LP_POSITIVE_HOLD_MIN_SOC_MARGIN_PCT: "Margem mínima de bateria (%)",
  LP_SOLAR_CHARGE_FOX_MODE: "Modo da Fox ao carregar com sol",
  OPTIMIZATION_PRESET: "Modo da casa (normal | hóspedes | férias)",
  VACATION_RETURN_AT: "Volta das férias (ISO local, ex. 2026-11-07T18:00)",
  VACATION_RECOVERY_HOURS: "Antecedência para religar o aquecimento (h)",
  VACATION_DAIKIN_HOLIDAY_ENABLED: "Usar modo férias da Daikin",
  DAIKIN_HOLIDAY_DETECT_ENABLED: "Detectar modo férias ligado na Daikin",
  DAIKIN_CONTROL_MODE: "HEM controla a Daikin? (passive | active)",
  DHW_TEMP_PV_ABUNDANCE_TARGET_C: "Teto do tanque com sol sobrando",
  DHW_TANK_OVERNIGHT_TARGET_C: "Meta do tanque na madrugada",
  DHW_SHOWER_COMFORT_C: "Temperatura mínima para o banho",
  DHW_MORNING_RESERVE_C: "Temperatura da reserva da manhã",
  DHW_MORNING_RESERVE_HOUR_LOCAL: "Hora da reserva da manhã",
  DHW_TANK_USABLE_FRACTION: "Fração útil do tanque",
  DHW_WARMUP_START_HOUR_LOCAL: "Início do aquecimento do tanque (h)",
  DHW_SETBACK_START_HOUR_LOCAL: "Início da redução do tanque (h)",
  DHW_WARMUP_PRICE_AWARE_ENABLED: "Aquecer o tanque seguindo o preço",
  DHW_WARMUP_WINDOW_START_LOCAL: "Janela de aquecimento: início (h)",
  DHW_WARMUP_WINDOW_END_LOCAL: "Janela de aquecimento: fim (h)",
  DHW_DYNAMIC_WINDOW_ENABLED: "Janela dinâmica do tanque",
  DHW_DYNAMIC_BOOST_HOLD_HOURS: "Horas de reforço mantido",
  DHW_SHOWERS_NORMAL_EVENING: "Banhos à noite (normal)",
  DHW_SHOWERS_NORMAL_MORNING_RESERVE: "Banhos de manhã (normal)",
  DHW_SHOWERS_EVENING_CAP: "Máximo de banhos à noite",
  DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST: "Banhos extras à noite por hóspede",
  DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST: "Banhos extras de manhã por hóspede",
  DHW_GUEST_COUNT: "Número de hóspedes",
  DHW_SHOWER_DURATION_MIN: "Duração do banho",
  DHW_SHOWER_FLOW_LPM: "Vazão do chuveiro",
  DHW_SHOWER_MIXER_TEMP_C: "Temperatura da mistura no chuveiro",
  DHW_SHOWER_COLD_INLET_TEMP_C: "Temperatura da água fria de entrada",
  DHW_LEGIONELLA_DAY: "Legionella: dia da semana",
  DHW_LEGIONELLA_HOUR_LOCAL: "Legionella: hora de início",
  DHW_LEGIONELLA_DURATION_MIN: "Legionella: duração",
  DHW_LEGIONELLA_TANK_TARGET_C: "Legionella: temperatura do tanque",
  REQUIRE_SIMULATION_ID: "Exigir simulação antes de aplicar em lote",
  LP_PLAN_PUSH_HOUR: "Envio do plano: hora (UTC)",
  LP_PLAN_PUSH_MINUTE: "Envio do plano: minuto (UTC)",
  MPC_FORECAST_REFRESH_INTERVAL_MINUTES: "Atualização da previsão (min)",
  PV_TELEMETRY_INTERVAL_MINUTES: "Intervalo da telemetria solar (min)",
  PV_CALIBRATION_WINDOW_DAYS: "Janela de calibração solar (dias)",
  WEATHER_LAT: "Latitude do local",
  WEATHER_LON: "Longitude do local",
  LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH: "Valor da bateria no fim do plano",
  LP_SOC_FINAL_KWH: "Meta de bateria no fim do plano",
};

export function labelFor(key: string): string {
  return PLAIN_COPY[key]?.label || KEY_LABELS[key] || key;
}

export const ALL_GROUPS: GroupSpec[] = [...VISIBLE_GROUPS, ...ADVANCED_GROUPS];

/** Every key placed in some group (visible or advanced). */
export function listedKeys(): string[] {
  return ALL_GROUPS.flatMap((g) => g.keys);
}

/** Registry keys that no group lists. The UI test asserts this is empty. */
export function unlistedKeys(registryKeys: string[]): string[] {
  const seen = new Set(listedKeys());
  return registryKeys.filter((k) => !seen.has(k));
}

/** Keys listed in more than one place (a bug: edits would be ambiguous). */
export function duplicateKeys(): string[] {
  const seen = new Set<string>();
  const dup = new Set<string>();
  for (const k of listedKeys()) (seen.has(k) ? dup : seen).add(k);
  return [...dup];
}

export interface SearchableSpec {
  key: string;
  description?: string;
}

/** Normalise for matching: lower-case, no accents. */
export function norm(s: string): string {
  return s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

/**
 * Advanced groups filtered by a free-text query over key, label, description
 * and group title (every whitespace-separated term must match). An empty query
 * keeps everything. Groups with no matching key are dropped. Keys absent from
 * `specs` (backend older than the UI) are skipped.
 */
export function filterAdvanced(
  groups: GroupSpec[],
  specs: Map<string, SearchableSpec>,
  query: string,
): GroupSpec[] {
  const terms = norm(query).split(/\s+/).filter(Boolean);
  const out: GroupSpec[] = [];
  for (const g of groups) {
    const keys = g.keys.filter((k) => {
      const spec = specs.get(k);
      if (!spec) return false;
      if (terms.length === 0) return true;
      const hay = norm(`${k} ${labelFor(k)} ${spec.description ?? ""} ${g.title}`);
      return terms.every((t) => hay.includes(t));
    });
    if (keys.length > 0) out.push({ ...g, keys });
  }
  return out;
}

export function unitFor(key: string): string {
  if (key.endsWith("_C")) return "°C";
  if (key.endsWith("_KWH")) return "kWh";
  if (key.endsWith("_MIN") || key.endsWith("_MINUTES")) return "min";
  if (key.endsWith("_MINUTE")) return "";
  if (key.endsWith("_HOUR") || key.endsWith("_HOUR_LOCAL") || key.endsWith("_HOURS")) return "h";
  if (key.endsWith("_LPM")) return "L/min";
  if (key.endsWith("_PENCE_PER_KWH")) return "p/kWh";
  if (key.endsWith("_DAYS")) return "d";
  if (key.endsWith("_DAY")) return "";
  if (key.endsWith("_FRACTION") || key.endsWith("_PERCENT")) return "";
  return "";
}

const TZ_SUFFIX = /(Z|[+-]\d\d:?\d\d)$/;

/**
 * "Normal" / "Férias até dom 12:00" for the read-only mode line. A naive ISO
 * return time (what VACATION_RETURN_AT stores) is local wall-clock and is shown
 * exactly as typed; an offset-carrying one is rendered in `tz`.
 * `travel`/`away` alias to vacation.
 */
export function modeLine(preset: unknown, returnAt: unknown, tz = "Europe/London"): string {
  const p = String(preset ?? "").toLowerCase();
  if (p === "guests") return "Hóspedes";
  if (!["vacation", "travel", "away"].includes(p)) return "Normal";
  const raw = String(returnAt ?? "").trim();
  if (!raw) return "Férias";
  const naive = !TZ_SUFFIX.test(raw);
  const d = new Date(naive ? raw + "Z" : raw);
  if (Number.isNaN(d.getTime())) return "Férias";
  const fmt = new Intl.DateTimeFormat("pt-BR", {
    weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
    hour12: false, timeZone: naive ? "UTC" : tz,
  });
  return `Férias até ${fmt.format(d).replace(",", "")}`;
}
