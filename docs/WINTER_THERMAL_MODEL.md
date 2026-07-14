# Winter thermal model — study, empirical parameters, and build plan

*2026-06-12. Prepared ahead of the user-installed indoor temperature sensors
landing. Goal: balance comfort vs savings in winter — never too hot, never too
cold, blankets acceptable overnight — using overnight setbacks WITHOUT a large
morning recovery delta (large deltas force high LWT, which collapses heat-pump
COP and defeats the point).*

> **Status (2026-07-13).** The study below is unchanged; the build plan has
> moved. **W1 (sensor ingestion) is SHIPPED** — see the "Indoor temperature
> sensor ingestion" section of `CLAUDE.md`. **W2 (thermal learner) is SHIPPED**
> (#641). **W3 (LP regains `t_in`) is SHIPPED but OFF by default** behind
> `LP_W3_TIN_ENABLED=false` (#657). The RC-fallback defaults are **no longer
> placeholders** (`BUILDING_UA_W_PER_K=600`, `BUILDING_THERMAL_MASS_KWH_PER_K=12.0`).
> W4 (validation) is the remaining phase. Per-item status is marked inline below.

---

## 1. Physics: the model HEM needs

### 1.1 First-order RC house model (lumped capacitance)

```
C · dT_in/dt = Q_heat + Q_gains − UA · (T_in − T_out)
```

- `UA` (kW/K) — whole-house heat-loss coefficient (fabric + ventilation).
  *Insulation quality lives here.*
- `C` (kWh/K) — effective thermal mass (fabric + contents).
- `τ = C / UA` (hours) — time constant: how fast the house coasts toward
  outdoor temperature with heating off. *This is the number that decides
  whether overnight setback is nearly-free or expensive.*
- Unheated decay: `T_in(t) = T_out + (T_in(0) − T_out) · e^(−t/τ)`.

### 1.2 Setback economics with a heat pump

Energy saved by a setback is proportional to the **average indoor-temperature
reduction** over the window:

```
ΔE_thermal ≈ UA · ∫(T_normal − T_actual) dt
ΔE_electric ≈ ΔE_thermal / COP
```

The heat-pump twist: recovery from a deep setback needs high heat-output rate
→ high LWT → **lower COP exactly when re-buying the heat**. COP falls roughly
2–3 %/K of extra lift. So the optimum for heat pumps is the opposite of the
gas-boiler instinct:

- **shallow setbacks** (1.5–3 °C), because the saving is linear in ΔT but the
  recovery-COP penalty grows with the delta;
- **slow, early recovery** (start 2–4 h before comfort time at low LWT) rather
  than a late blast;
- recovery **scheduled into cheap slots** whenever the tariff allows — the LP's
  natural job.

### 1.3 What "blanket mode" is worth in THIS house

With the empirically measured `UA ≈ 0.6 kW/K` (§2):

| scenario | thermal saved | electric (COP 3) | winter £ (20–30 p) |
|---|---|---|---|
| −1 °C average, 8 h night | 4.8 kWh | 1.6 kWh | 32–48 p/night |
| −2.5 °C average, 8 h night (blankets) | 12 kWh | 4.0 kWh | 80–120 p/night |

A whole winter (Nov–Mar, ~150 nights) of a disciplined 2 °C-average setback is
roughly **£75–£140**. This is the single biggest untapped lever after the
battery — IF the recovery is done gently (otherwise the COP penalty claws back
30–50 % of it).

---

## 2. Empirical parameters measured from this house's own data

### 2.1 Heat-loss coefficient UA (measured 2026-06-12)

Regression of `fox_energy_daily.load_kwh` vs heating degree-days (Open-Meteo
archive daily means for W4 1DZ, base 15.5 °C), 2026-01-01 → 2026-04-30,
**120 days, R² = 0.66**:

```
load_kwh/day ≈ 7.2 + 5.02 × HDD
```

- **Slope 5.0 kWh-electric per °C·day** → `UA_eff ≈ 520–730 W/K` for COP
  2.5–3.5 (≈ **630 W/K at COP 3**).
- Intercept 7.2 kWh/day = base load + DHW (sanity: June daily total ≈ 13–19,
  of which DHW ≈ 2–3 — consistent within regression noise).
- Coldest day on record (−1.4 °C mean): **107 kWh** household load.

Caveats: COP is assumed, not measured (`cop_daily` is never populated — see
follow-up in #238 precursor); solar/internal gains are folded into the
intercept, so UA is an *effective* number. Good enough to size decisions;
the sensor data will refine it.

**Context:** 500–700 W/K is high (typical UK semi: 200–300). Either the house
is large, or there is real insulation headroom — possibly both. Every
insulation improvement shows up directly in the slope of this regression,
which HEM can re-run monthly as a free "did the loft insulation work?" audit.

### 2.2 Time constant τ — NOT measurable yet

There has never been an indoor temperature source: `daikin_room_temp` is NULL
in 100 % of execution_log rows, and `daikin_telemetry.indoor_temp_c` is 0/606
on live reads (the Altherma has no room stat; the 73 non-null rows are the
estimator's own synthetic output). **The new sensors are the first real
measurement.** τ falls out of the very first cold nights of data (slope of
overnight decay vs ΔT in/out).

Until measured, assume τ ≈ 15–30 h (UK masonry at this UA with
C ≈ 10–20 kWh/K) and let the learner replace it.

### 2.3 A cautionary tale already in production (June 2026)

The price-tier LWT pre-heat (lab-enabled 2026-06-06) raised measured space
heating from **0 → 3–8 kWh/day in June** (~34 kWh in week 1) because:

- the LP gives `e_space` **no objective cost and no modeled benefit** — it is
  a sink for cheap energy bounded by the weather-curve corridor;
- the pre-heat "benefit" (store heat now, need less later) was **asserted, not
  modeled** — there was no `t_in` state to store it in (removed in Phase B
  #310, correctly, because there was nothing to measure it against). *W3 (#657)
  has since restored `t_in` with the RC dynamics constraint, seeded by the
  sensor — but it is gated behind `LP_W3_TIN_ENABLED`, default `false`, so the
  "no thermal state" critique still applies to every solve until that flag is
  flipped on;*
- the summer guard had been removed, and a positive offset *wakes* the
  compressor that the firmware would have left off;
- the `k_per_degc` calibration then **learned from the heating the offsets
  themselves caused** (k drifted 0.033 → 0.067, near its clamp).

Lesson encoded in this plan: *no thermal actuation without a thermal state,
and no calibration that can eat its own output.*

---

## 3. What is in place vs what must be built

### 3.1 Already in place (keep)

| component | where | state |
|---|---|---|
| Weather-curve LWT model (`get_lwt_base_c`) | physics.py:126 | live |
| `k_per_degc` LWT→kW calibration + table | physics.py:25, db `daikin_lwt_kw_calibration` | live (needs decontamination, §3.3) |
| LWT offset dispatch + restore rows + drift backstop | lp_dispatch.py:432–525, #492 | live |
| `smooth_lwt_offsets` anti-chatter (min 2 h blocks) | lp_dispatch.py:376 | live |
| Comfort guard (suppress boost/setback by `indoor_c`) | lp_dispatch.py | **ACTIVE** — W1 shipped, the ESPHome sensor reports and the guard reads the freshest in-band value (`INDOOR_SENSOR_STALE_MINUTES=30`) |
| `INDOOR_SETPOINT_C` (runtime-tunable), `INDOOR_COMFORT_BAND_C` | config.py | live |
| Estimator RC fallback (`BUILDING_UA_W_PER_K`, `BUILDING_THERMAL_MASS_KWH_PER_K`) | estimator.py, config.py | live, **defaults now match measurement: UA = 600 W/K, C = 12.0 kWh/K** (the 180 / 8 placeholders are gone — §3.3) |
| Indoor sensor ingestion (`POST /api/v1/sensors/indoor`, `room_temperature_history`, `device_reading_log`) | api/routers/sensors.py | **live (W1)** |
| Thermal learner (`analytics/thermal_learning.py`, `building_thermal_calibration`) | 05:30 UTC cron | **live (W2, #641)** |
| LP indoor state `t_in[i]` + comfort slack | lp_optimizer.py | **shipped (W3, #657) but OFF** — `LP_W3_TIN_ENABLED=false` by default |
| Calibration-loop house pattern (PV recent-bias #486, DHW auto-scale #534) | — | the template the thermal learner should follow |

### 3.2 To build (the winter epic)

**Phase W1 — sensor ingestion — ✅ SHIPPED.** Delivered as specified, plus a
lossless per-device audit sink (`device_reading_log`) and a scoped
`HEM_SENSOR_INGEST_TOKEN` so the internet-exposed ESPHome sensor can only POST
to the one route. Read-back: `GET /api/v1/sensors/indoor|devices|device-log`.
See CLAUDE.md §"Indoor temperature sensor ingestion" and `deploy/README.md` §12.
Original spec, for the record:
1. Table `room_temperature_history(captured_at PK, temp_c, room, source, quality)` —
   multi-room from day one (cheap now, painful later).
2. `POST /api/v1/sensors/indoor` (admin bearer; batch-friendly payload) +
   matching MCP tool. Clone the `save_pv_realtime_sample` idempotent pattern.
3. Wire the freshest reading into: LP initial state (`indoor_source="sensor"`),
   the existing dispatch comfort guard (turns it on for free), cockpit chart.
4. Staleness semantics: sensor older than N minutes → treat as absent
   (fall back to estimator), surface a chip in the UI.

**Phase W2 — thermal learner (after ~2 weeks of sensor data, cold nights help)**
5. `analytics/thermal_learning.py`: τ from unheated overnight decay episodes;
   UA re-fit from the HDD regression (§2.1, now with measured indoor ΔT
   instead of an assumed base temperature); `C = τ·UA`. Persist to a
   `building_thermal_calibration` table with R², n, window — same shape as
   `daikin_lwt_kw_calibration`. Quality gate: only trust below documented
   residual thresholds; estimator + LP read learned values with env fallback.
   **(SHIPPED 2026-07-05, code-ready pre-sensors.)** Pure fitters tested
   against synthetic decay curves; contamination = heating buckets + LWT
   offset windows + a `THERMAL_TAU_SETTLE_HOURS` (2 h) margin for
   radiator/hydronic after-emission; ΔT ≥ 5 °C physics gate. τ-first design:
   the UA HDD re-fit gates itself on ≥ 20 heating-season days and stays
   `skipped` through summer. Cron 05:30 UTC; estimator consumes the bounded
   readers; night brief gains a thermal line;
   `GET /api/v1/sensors/thermal-calibration` shows learned + effective values.
   Kill switches: `THERMAL_LEARNING_ENABLED` (cron),
   `THERMAL_LEARNED_VALUES_ENABLED` (readers → env fallback).
6. **Decontaminate `k_per_degc`**: exclude slots inside HEM-commanded offset
   windows from the regression sample (it must learn the firmware's natural
   behaviour, not HEM's own echo).

**Phase W3 — LP re-gains a thermal state (the real prize) — ✅ SHIPPED (#657),
but DEFAULT-OFF behind `LP_W3_TIN_ENABLED=false`.** Items 7–10 are implemented
(`t_in[i]` RC dynamics, night comfort floor `LP_W3_NIGHT_FLOOR_C=17.5`,
gentle-recovery cap `LP_W3_MAX_RECOVERY_C_PER_SLOT=0.5`, comfort penalty
`LP_W3_COMFORT_PEN_PENCE_PER_DEGC_SLOT=15`). Flip the flag on before the heating
season and validate with W4.
7. Restore `t_in[i]` with the RC dynamics constraint (reference: Phase B
   removal commit), driven by learned UA/C, seeded by the sensor.
8. **Time-varying comfort band** — the "blanket schedule":
   `COMFORT_BAND_SCHEDULE` runtime setting, e.g. day 20.5±1 °C,
   night (23:00–06:30) floor 17.5 °C. Soft constraints (slack + penalty),
   never hard-infeasible.
9. **Gentle-recovery constraint**: cap indoor rise per slot (e.g. ≤0.5 °C/h)
   and/or cap recovery LWT offset, so the solver structurally cannot choose
   the big-morning-delta anti-pattern; recovery start time becomes the LP's
   decision (it will naturally pick cheap pre-dawn slots).
10. e_space gains its real coupling: heat delivered to `t_in` via COP(T_out,
    LWT); the offset stops being a price heuristic and becomes the *actuator*
    of an optimized trajectory. The #482 tier heuristic is then retired (the
    peak setback −2 °C can stay as interim until W3 lands).
11. **Demand gate now** (independent of sensors): no positive LWT offset when
    trailing-48 h measured `kwh_heating ≈ 0` — stops the June waste pattern
    permanently, regardless of season. **(Shipped #541.)**
11b. **Outdoor cutoff + thermal-lag tail (SHIPPED — `fix/lwt-outdoor-cutoff-…`).**
    The #541 demand gate alone proved foolable in prod: positive offsets woke
    the compressor (heating 0.0 → ~4.6 kWh/day from Jun 5, 2026), and the
    HEM-induced heat bled into the 2-h bucket *after* each offset window —
    counted as natural demand, latching the gate open (`measured_window_kwh`
    sat at 1.0 just over the 0.5 floor). Two fixes: (a) an **exogenous outdoor
    cutoff** (`DAIKIN_LWT_PREHEAT_OUTDOOR_CUTOFF_C`, default 15 °C) suppresses
    POSITIVE offsets per-slot — the self-loop can't fake the weather; the −2
    setback is never cut. (b) the decontamination excludes
    `DAIKIN_LWT_PREHEAT_DECONTAM_TAIL_BUCKETS` (default 1) trailing buckets per
    window. Verified on the prod DB: `measured_window_kwh` 1.0 → 0.0 (demand
    gate now reads closed) and `positive_offset_suppressed_by_outdoor` → true at
    20 °C. (`space_heating_gate_state` keeps `preheat_suppressed` scoped to the
    demand gate — "all LWT off" — distinct from the warm-day positive-only flag.)

**Phase W4 — validate before the heating season peaks**
12. Replay scorecard: simulated winter days, plan-vs-actual indoor trajectory
    once real cold arrives; brief line "comfort: min overnight X °C (floor Y)".
13. Insulation audit view: monthly UA slope re-fit + trend — measures both
    insulation work and the model's honesty.

### 3.3 Quick wins shippable immediately — all DONE

- `BUILDING_UA_W_PER_K` 180 → ~600 (measured) and
  `BUILDING_THERMAL_MASS_KWH_PER_K` 8 → ~12 (τ ≈ 20 h prior) so the existing
  estimator fallback stops being 3.5× optimistic. **DONE — these are the code
  defaults in `src/config.py` now (600 / 12.0).**
- The W3-item-11 demand gate (small PR, kills the active June waste). **DONE #541.**
- Outdoor cutoff on positive offsets + thermal-lag tail exclusion (closes the
  residual self-loop #541 left open). **DONE — see item 11b.**
- `k_per_degc` decontamination filter (small PR). **DONE #541.**

---

## 4. Design principles (carried from the household's stated constraints)

1. **Comfort floor is sacred, savings are best-effort** — soft constraints
   with steep penalties; an Infeasible plan must never mean a cold house.
   Firmware weather curve remains the safety net (HEM only nudges the offset).
2. **Never buy a big morning delta** — gentle-recovery cap is structural, not
   advisory (§3.2 item 9).
3. **No actuation without measurement** — positive offsets require a live
   indoor sensor OR measured heating demand; calibrations must exclude
   HEM-caused load.
4. **Blankets are a feature** — the night comfort band is a user-tunable
   runtime setting, visible in the cockpit, not a buried constant.
