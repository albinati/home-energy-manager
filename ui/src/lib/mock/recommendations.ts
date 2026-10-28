import type { RecommendationsResponse } from "../recTypes";

// Dev-only fixture for GET /api/v1/insights/recommendations, reachable ONLY via
// `?mock=1` on /insights (see routes/insights.tsx). Mirrors the approved mockup.
export const MOCK_RECOMMENDATIONS: RecommendationsResponse = {
  generated_at: "2026-10-28T12:00:00Z",
  week: { start: "2026-10-26", end: "2026-10-28", complete_days: 2, last_complete: "2026-10-27" },
  items: [
    { key: "washer", who: "Máquina de lavar", icon: "washer", kind: "status", money_pence: null, money_label: "ok", action: null,
      headline: "Automática — o HEM liga no melhor horário; hoje 13:00",
      detail: "É só apertar Smart Control na máquina. Esta semana: 3 lavagens, todas na faixa barata, 0,33 kWh cada — custaram 12p no total (teriam custado 31p sem o HEM)." },
    { key: "breakfast", who: "Café da manhã", icon: "breakfast", kind: "suggestion", money_pence: -35, money_label: "−35p/sem", action: null,
      headline: "Chaleira e forno antes das 07:00",
      detail: "04–07 é faixa barata (12,5p); às 07:00 começa a faixa dia (25,5p). Nas últimas 28 manhãs a rede forneceu ~0,4 kWh por dia além do consumo de base entre 07:00 e 08:30." },
    { key: "lunch_weekend", who: "Almoço (fim de semana)", icon: "lunch", kind: "suggestion", money_pence: -35, money_label: "−35p/almoço", action: null,
      headline: "Cozinhar a partir das 13:00",
      detail: "Nos últimos 8 dias de fim de semana, 12:00–13:00 comprou da rede ~2,6 kWh além do consumo de base, a 25,5p; às 13:00 a faixa barata (12,5p) começa." },
    { key: "dinner", who: "Jantar", icon: "dinner", kind: "suggestion", money_pence: -10, money_label: "−10p/sem", action: null,
      headline: "A bateria cobre o jantar 6 de 7 dias — a terça é o problema",
      detail: "Terça: nas últimas 4 semanas o pico (16–19) comprou ~0,8 kWh da rede a 38p em 3 de 4 dias. O consumo no pico veio acima do previsto. Jantar depois das 19:00 nesse dia custa 25p em vez de 38p por kWh." },
    { key: "showers", who: "Banhos", icon: "drop", kind: "status", money_pence: null, money_label: "ok", action: null,
      headline: "Água a 45 °C das 13:00 às 22:00; depois cai para 37",
      detail: "Cabem 4 banhos a 45 °C. Domingo 12:00–14:30 ciclo anti-legionella (60 °C)." },
    { key: "tune_DHW_TEMP_NORMAL_C", who: "Água quente", icon: "drop", kind: "setting", money_pence: -9, money_label: "−9p/sem",
      action: { setting_key: "DHW_TEMP_NORMAL_C", from: 45, to: 44 },
      headline: "Baixar o alvo do tanque 45 → 44 °C",
      detail: "Revisão de domingo: 7 dias simulados, mesmo conforto." },
    { key: "tune_LP_W3_NIGHT_FLOOR_C", who: "Aquecimento", icon: "thermo", kind: "setting", money_pence: -32, money_label: "−32p/sem",
      action: { setting_key: "LP_W3_NIGHT_FLOOR_C", from: 20, to: 19.5 },
      headline: "Piso noturno 20 → 19,5 °C",
      detail: "Revisão de domingo: 7 dias simulados, 0,3 h a mais abaixo do piso. Você disse ok 5× e frio 1× (cozinha, 06:40)." },
  ],
  week_summary: {
    complete_days: 2, net_gbp: 2.7, usual_gbp: 3.1, current_label: "Cosy",
    compare: [{ short_label: "Agile", delta_gbp: 1.8 }, { short_label: "Flux", delta_gbp: 0.9 }, { short_label: "fixo", delta_gbp: 3.2 }],
    energy: {
      total_kwh: 112,
      by_band: { cheap: 58, day: 26, peak: 0, solar: 28 },
      shares: { cheap: 0.518, day: 0.232, peak: 0, solar: 0.25 },
      peak_kwh: 0,
    },
    solar: { kwh: 23, self_use_share: 0.61, export_kwh: 9, export_gbp: 1.35, forecast_mape: 0.12 },
    battery: { arbitrage_gbp: 3.9, cheap_bought_kwh: 58, cheap_p: 12.49, cycles_per_day: 1.0, peak_covered_days: 6, days: 7 },
  },
  house: {
    cooling_c_per_h: { value: 0.06, outdoor_c: 9, tau_h: 70 },
    heating_c_per_h: { value: 0.16, offset_c: 3, lag_min: 35 },
    solar_gain: { coefficient: 0.2, value_c_per_sunny_afternoon: 0.4, applied: false },
    tank: { c_per_h_night: 0.35, ratio_median_recent: 1.02 },
  },
};
