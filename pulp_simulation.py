import pulp

# --- CONFIGURAÇÃO DA SIMULAÇÃO ---
PERIODS = 24  # 12 horas (blocos de 30 min)
BATTERY_CAPACITY = 10.0  # kWh
MAX_INVERTER_POWER = 3.0  # kW (ou 1.5 kWh por slot)
INITIAL_SOC = 2.0  

# Preços Agile
prices = [12, 10, 5, 2, -1, -5, 5, 10, 15, 30, 35, 40, 35, 30, 20, 15, 15, 12, 10, 10, 10, 10, 10, 10]
base_load = [0.5] * PERIODS
solar = [0]*12 + [0.5, 1.0, 1.5, 2.0, 2.0, 1.5, 1.0, 0.5] + [0]*4

prob = pulp.LpProblem("HomeEnergyOptimization", pulp.LpMinimize)

# Variáveis (kWh)
p_import = pulp.LpVariable.dicts("Import", range(PERIODS), lowBound=0, upBound=5.0) # Fusível 100A /2 ~ 5kWh slot
p_export = pulp.LpVariable.dicts("Export", range(PERIODS), lowBound=0, upBound=3.0) # Limite DNO ~ 3kWh slot
b_charge = pulp.LpVariable.dicts("Charge", range(PERIODS), lowBound=0, upBound=MAX_INVERTER_POWER*0.5)
b_discharge = pulp.LpVariable.dicts("Discharge", range(PERIODS), lowBound=0, upBound=MAX_INVERTER_POWER*0.5)
soc = pulp.LpVariable.dicts("SoC", range(PERIODS+1), lowBound=0, upBound=BATTERY_CAPACITY)

prob += soc[0] == INITIAL_SOC

total_cost = 0
for i in range(PERIODS):
    # Física: Bateria
    prob += soc[i+1] == soc[i] + b_charge[i] - b_discharge[i]
    
    # Física: Balanço de Energia
    prob += p_import[i] + solar[i] + b_discharge[i] == base_load[i] + p_export[i] + b_charge[i]
    
    # Custo: Importação * Preço Dinâmico - Exportação * 15p fixo
    total_cost += p_import[i] * prices[i] - p_export[i] * 15  

prob += total_cost
prob.solve(pulp.PULP_CBC_CMD(msg=False))

print(f"Status do Solver: {pulp.LpStatus[prob.status]}")
print(f"Lucro/Custo Total Projetado: {pulp.value(prob.objective):.2f} pence\n")
print("Slot | Preco | Solar | Import | Export | Carga Bat | Descar Bat | SoC Final")
print("-" * 80)
for i in range(PERIODS):
    print(f"{i:02d}   | {prices[i]:5.1f} | {solar[i]:5.1f} | "
          f"{p_import[i].varValue:6.2f} | {p_export[i].varValue:6.2f} | "
          f"{b_charge[i].varValue:9.2f} | {b_discharge[i].varValue:10.2f} | {soc[i+1].varValue:9.2f}")
