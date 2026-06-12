# Winter thermal model — study, empirical parameters, and build plan

*2026-06-12. Prepared ahead of the user-installed indoor temperature sensors
landing. Goal: balance comfort vs savings in winter — never too hot, never too
cold, blankets acceptable overnight — using overnight setbacks WITHOUT a large
morning recovery delta (large deltas force high LWT, which collapses heat-pump
COP and defeats the point).*

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
- the pre-heat "benefit" (store heat now, need less later) is **asserted, not
  modeled** — there is no `t_in` state to store it in (removed in Phase B
  #310, correctly, because there was nothing to measure it against);
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
| **Sensor-ready comfort guard** (suppress boost/setback by `indoor_c`) | lp_dispatch.py:329–373 | live but **no-op** (`indoor_c=None`) — activates the day a sensor reports |
| `INDOOR_SETPOINT_C` (runtime-tunable), `INDOOR_COMFORT_BAND_C` | config.py:1468, 1263 | live |
| Estimator RC fallback (`BUILDING_UA_W_PER_K`, `BUILDING_THERMAL_MASS_KWH_PER_K`) | estimator.py:72–133, config.py:1259–1261 | live, **placeholder UA=180 W/K is ~3.5× below measured** |
| Phase B removed `t_in[i]`/comfort-slack LP code | `git show daa5beb` | recoverable reference implementation |
| Calibration-loop house pattern (PV recent-bias #486, DHW auto-scale #534) | — | the template the thermal learner should follow |

### 3.2 To build (the winter epic)

**Phase W1 — sensor ingestion (do first; everything downstream feeds on it)**
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
6. **Decontaminate `k_per_degc`**: exclude slots inside HEM-commanded offset
   windows from the regression sample (it must learn the firmware's natural
   behaviour, not HEM's own echo).

**Phase W3 — LP re-gains a thermal state (the real prize)**
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
    permanently, regardless of season.

**Phase W4 — validate before the heating season peaks**
12. Replay scorecard: simulated winter days, plan-vs-actual indoor trajectory
    once real cold arrives; brief line "comfort: min overnight X °C (floor Y)".
13. Insulation audit view: monthly UA slope re-fit + trend — measures both
    insulation work and the model's honesty.

### 3.3 Quick wins shippable immediately

- `BUILDING_UA_W_PER_K` 180 → ~600 (measured) and
  `BUILDING_THERMAL_MASS_KWH_PER_K` 8 → ~12 (τ ≈ 20 h prior) so the existing
  estimator fallback stops being 3.5× optimistic.
- The W3-item-11 demand gate (small PR, kills the active June waste).
- `k_per_degc` decontamination filter (small PR).

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
