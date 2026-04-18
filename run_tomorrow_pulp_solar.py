import pulp
import sqlite3

conn = sqlite3.connect('/root/home-energy-manager/data/energy_state.db')
cursor = conn.cursor()

cursor.execute("""
    SELECT valid_from, value_inc_vat 
    FROM agile_rates 
    WHERE valid_from >= '2026-04-19T00:00:00Z' AND valid_from < '2026-04-20T00:00:00Z'
    ORDER BY valid_from ASC 
""")
rows = cursor.fetchall()
real_prices = [row[1] for row in rows]
time_labels = [row[0][11:16] for row in rows]
PERIODS = len(real_prices)

target_slot_morning = next((i for i, t in enumerate(time_labels) if t == "07:00"), 14)
target_slot_evening = next((i for i, t in enumerate(time_labels) if t == "20:00"), 40)

# --- FÍSICA ---
BATTERY_CAPACITY = 10.0
MAX_INVERTER = 1.5 # kWh por slot (3kW inverter)
INITIAL_SOC = 2.0  
INITIAL_TANK_TEMP = 40.0 

DAIKIN_ELEC_MAX = 1.5 
THERMAL_MULTIPLIER = 10.0 
THERMAL_DECAY = 0.165

base_load = [0.5] * PERIODS

# Curva Solar Empírica (baseada nos 3kW de pico que vc citou para a primavera)
# Transformando em kWh gerados POR SLOT DE 30 MINUTOS
# Se o pico é ~3kW às 13h, o inversor gera 1.5kWh naquele bloco de meia hora.
solar_kwh_per_slot = []
for t in time_labels:
    h = int(t.split(':')[0])
    if h < 6 or h > 19:
        solar_kwh_per_slot.append(0.0)
    elif 6 <= h < 9:
        solar_kwh_per_slot.append(0.3)
    elif 9 <= h < 11:
        solar_kwh_per_slot.append(0.8)
    elif 11 <= h <= 14:
        solar_kwh_per_slot.append(1.5) # Pico do sol do meio-dia (3kW)
    elif 15 <= h < 17:
        solar_kwh_per_slot.append(1.0)
    elif 17 <= h <= 19:
        solar_kwh_per_slot.append(0.4)
    else:
        solar_kwh_per_slot.append(0.0)

prob = pulp.LpProblem("HomeEnergy_Daikin_Tomorrow", pulp.LpMinimize)

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
    # BALANÇO COM SOLAR REALISTA
    prob += p_import[i] + solar_kwh_per_slot[i] + b_discharge[i] == base_load[i] + p_export[i] + b_charge[i] + p_daikin[i]
    prob += t_tank[i+1] == t_tank[i] + (p_daikin[i] * THERMAL_MULTIPLIER) - THERMAL_DECAY
    total_cost += p_import[i] * real_prices[i] - p_export[i] * 15.0

prob += t_tank[target_slot_morning] >= 48.0
prob += t_tank[target_slot_evening] >= 48.0

prob += total_cost
prob.solve(pulp.PULP_CBC_CMD(msg=False))

print(f"Status: {pulp.LpStatus[prob.status]}")
print(f"LUCRO/CUSTO PROJETADO AMANHÃ: {pulp.value(prob.objective):.2f} pence\n")
print(f"Hora  | Preco | Solar | Import | Export | CargaB | DescarB | Daikin | SoC | Tanque")
print("-" * 90)
for i in range(PERIODS):
    flag = ""
    if i == target_slot_morning: flag = " < BANHO MANHA"
    elif i == target_slot_evening: flag = " < BANHO NOITE"
    
    if i in [0, 4, 10, 14, 20, 24, 29, 30, 31, 32, 40, 47] or p_daikin[i].varValue > 0 or p_export[i].varValue > 0:
        print(f"{time_labels[i]} | {real_prices[i]:5.1f} | {solar_kwh_per_slot[i]:5.1f} | "
              f"{p_import[i].varValue:6.2f} | {p_export[i].varValue:6.2f} | "
              f"{b_charge[i].varValue:6.2f} | {b_discharge[i].varValue:7.2f} | "
              f"{p_daikin[i].varValue:6.2f} | {soc[i+1].varValue:3.1f}| {t_tank[i+1].varValue:4.1f}C" + flag)
