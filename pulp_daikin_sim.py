import pulp
import sqlite3
import datetime

conn = sqlite3.connect('/root/home-energy-manager/data/energy_state.db')
cursor = conn.cursor()
now_iso = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

# Pegar as próximas 18 horas (36 slots) para chegar até amanhã cedo
cursor.execute("""
    SELECT valid_from, value_inc_vat 
    FROM agile_rates 
    WHERE valid_from >= ?
    ORDER BY valid_from ASC 
    LIMIT 36
""", (now_iso,))
rows = cursor.fetchall()
real_prices = [row[1] for row in rows]
time_labels = [row[0][11:16] for row in rows]
PERIODS = len(real_prices)

target_slot = next((i for i, t in enumerate(time_labels) if t == "07:00"), PERIODS - 1)

# --- FÍSICA ---
BATTERY_CAPACITY = 10.0
MAX_INVERTER = 1.5 # kWh por slot de 30m
INITIAL_SOC = 10.0
INITIAL_TANK_TEMP = 46.0 

# Termodinâmica estimada: 1 kWh elétrico na Daikin (COP~3) = ~10°C de aumento num tanque de 200L
DAIKIN_ELEC_MAX = 1.5 
THERMAL_MULTIPLIER = 10.0 
THERMAL_DECAY = 0.25 # °C perdido por meia hora (0.5°C por hora)
base_load = [0.5] * PERIODS

prob = pulp.LpProblem("HomeEnergy_Daikin", pulp.LpMinimize)

p_import = pulp.LpVariable.dicts("Import", range(PERIODS), lowBound=0, upBound=5.0)
p_export = pulp.LpVariable.dicts("Export", range(PERIODS), lowBound=0, upBound=3.0)
b_charge = pulp.LpVariable.dicts("Charge", range(PERIODS), lowBound=0, upBound=MAX_INVERTER)
b_discharge = pulp.LpVariable.dicts("Discharge", range(PERIODS), lowBound=0, upBound=MAX_INVERTER)
soc = pulp.LpVariable.dicts("SoC", range(PERIODS+1), lowBound=0, upBound=BATTERY_CAPACITY)
p_daikin = pulp.LpVariable.dicts("Daikin", range(PERIODS), lowBound=0, upBound=DAIKIN_ELEC_MAX)
t_tank = pulp.LpVariable.dicts("TankTemp", range(PERIODS+1), lowBound=20.0, upBound=65.0)

prob += soc[0] == INITIAL_SOC
prob += t_tank[0] == INITIAL_TANK_TEMP

total_cost = 0
for i in range(PERIODS):
    prob += soc[i+1] == soc[i] + b_charge[i] - b_discharge[i]
    prob += p_import[i] + b_discharge[i] == base_load[i] + p_export[i] + b_charge[i] + p_daikin[i]
    prob += t_tank[i+1] == t_tank[i] + (p_daikin[i] * THERMAL_MULTIPLIER) - THERMAL_DECAY
    total_cost += p_import[i] * real_prices[i] - p_export[i] * 15.0

# REGRAS DO BANHO
prob += t_tank[target_slot] >= 40.0

prob += total_cost
prob.solve(pulp.PULP_CBC_CMD(msg=False))

print(f"Status: {pulp.LpStatus[prob.status]}")
print(f"Custo Projetado: {pulp.value(prob.objective):.2f} pence\n")
print(f"Hora  | Preco | Import | Export | CargaB | DescarB | Daikin(kW)| SoC | Tanque")
print("-" * 85)
for i in range(PERIODS):
    flag = " < BANHO 40C" if i == target_slot else ""
    print(f"{time_labels[i]} | {real_prices[i]:5.1f} | "
          f"{p_import[i].varValue:6.2f} | {p_export[i].varValue:6.2f} | "
          f"{b_charge[i].varValue:6.2f} | {b_discharge[i].varValue:7.2f} | "
          f"{p_daikin[i].varValue:9.2f} | {soc[i+1].varValue:4.1f}| {t_tank[i+1].varValue:4.1f}C" + flag)
