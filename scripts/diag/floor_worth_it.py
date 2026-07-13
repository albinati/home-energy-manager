"""Reconcile the pessimistic charge floor: premium paid vs value delivered.

The floor (#615, newsvendor) buys insurance against "battery empty at the evening
peak". We measure the premium it charges (the LP's own objective delta, logged as
`insurance_cost_pence`) against the event it insures, day by day:

  * PAYOFF POSSIBLE  — the battery came close to empty during peak hours, so the
    extra charge the floor forced was (or could have been) genuinely needed.
  * CANNOT PAY OFF   — the battery hit ~full BEFORE the peak *and* PV surplus was
    still being exported. The insured event is structurally impossible that day:
    the battery would have filled from free PV anyway, so the premium bought a
    protection that could never trigger. This is pure waste.

The #684 audit concluded "leave as-is, <=50p/yr recoverable" using empty-at-peak as
the metric. This asks the complementary question the audit didn't: what does the
premium COST on the days where the insurance cannot possibly pay?
"""
import sqlite3, json
c = sqlite3.connect('/app/data/energy_state.db')
c.row_factory = sqlite3.Row

RESERVE = 10.0        # MIN_SOC_RESERVE_PERCENT
NEAR_EMPTY = 20.0     # "came close to the floor"
NEAR_FULL = 97.0

# --- premium per day: the LAST committed run of each day (the plan that ran) ---
premium = {}
binding = {}
for r in c.execute("""SELECT date(run_at_utc) d, exogenous_snapshot_json
                      FROM lp_inputs_snapshot WHERE run_at_utc >= date('now','-40 days')
                      ORDER BY run_id"""):
    try:
        cf = (json.loads(r['exogenous_snapshot_json'] or '{}') or {}).get('pess_charge_floor')
    except Exception:
        cf = None
    if cf:
        premium[r['d']] = float(cf.get('insurance_cost_pence') or 0)
        binding[r['d']] = int(cf.get('binding_slots') or 0)

print(f"{'day':11} {'prem_p':>7} {'bind':>5} {'minSoC@peak':>12} {'full_before_peak':>17} "
      f"{'PV exported after full':>23}  verdict")
tot_prem = tot_waste = 0.0
n_payoff = n_waste = n_other = 0

for d in sorted(premium):
    # peak hours = the dear evening block, 15:00-20:00 UTC (16:00-21:00 BST)
    row = c.execute("""SELECT MIN(soc_pct) mn FROM pv_realtime_history
                       WHERE date(captured_at)=? AND time(captured_at) BETWEEN '15:00' AND '20:00'""",
                    (d,)).fetchone()
    min_peak = row['mn'] if row and row['mn'] is not None else None

    # did the battery reach ~full BEFORE the peak began?
    row = c.execute("""SELECT MAX(soc_pct) mx FROM pv_realtime_history
                       WHERE date(captured_at)=? AND time(captured_at) < '15:00'""", (d,)).fetchone()
    max_pre = row['mx'] if row and row['mx'] is not None else None

    # was PV still being exported once the battery was full? (=> it'd have filled anyway)
    row = c.execute("""SELECT ROUND(SUM(grid_export_kw)*(3.0/60.0),2) kwh FROM pv_realtime_history
                       WHERE date(captured_at)=? AND time(captured_at) BETWEEN '10:00' AND '17:00'
                         AND soc_pct >= ? AND grid_export_kw > 0.05""", (d, NEAR_FULL)).fetchone()
    exp_after_full = row['kwh'] if row and row['kwh'] is not None else 0.0

    p = premium[d]
    tot_prem += p

    if min_peak is not None and min_peak <= NEAR_EMPTY:
        verdict, n_payoff = "PAYOFF POSSIBLE (ran near empty at peak)", n_payoff + 1
    elif max_pre is not None and max_pre >= NEAR_FULL and exp_after_full > 0.3:
        verdict = "CANNOT PAY OFF (full pre-peak + PV exported)"
        n_waste += 1
        tot_waste += p
    else:
        verdict, n_other = "inconclusive", n_other + 1

    mp = f"{min_peak:.0f}%" if min_peak is not None else "n/a"
    xp = f"{max_pre:.0f}%" if max_pre is not None else "n/a"
    print(f"{d:11} {p:>7.2f} {binding[d]:>5} {mp:>12} {xp:>17} {exp_after_full:>21.2f} kWh  {verdict}")

n = len(premium)
print(f"\n--- {n} days with the floor binding ---")
print(f"total premium paid              : {tot_prem:6.1f}p   ({tot_prem/max(n,1):.2f}p/day)")
print(f"  annualised at this rate       : £{tot_prem/max(n,1)*365/100:.2f}/yr")
print(f"\ndays the insurance COULD pay off : {n_payoff}")
print(f"days it structurally CANNOT      : {n_waste}   (premium burnt: {tot_waste:.1f}p"
      f" = {100*tot_waste/max(tot_prem,1e-9):.0f}% of all premium)")
print(f"inconclusive                     : {n_other}")
print(f"\nIf the floor simply did not bind on the 'cannot pay off' days,")
print(f"the saving would be ~{tot_waste/max(n,1)*365/100:.2f} GBP/yr, with the winter")
print(f"protection on the other days fully intact.")
c.close()
