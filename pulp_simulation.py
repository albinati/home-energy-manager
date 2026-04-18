import pulp

# --- CONFIGURAÇÃO DA SIMULAÇÃO ---
PERIODS = 24  # Simulando 12 horas (24 blocos de meia hora)
BATTERY_CAPACITY = 10.0  # Bateria FoxESS de 10kWh
MAX_POWER = 3.0  # Inversor aguenta transferir ~3kW por hora (1.5kWh por slot)
INITIAL_SOC = 1.0  # Bateria começa quase vazia

# Preços fictícios da Octopus Agile (pence/kWh)
# Repare nos slots 4 e 5: Preços NEGATIVOS (Madrugada)
# Repare nos slots 9 a 13: Pico absoluto (Fim de tarde)
prices = [12, 10, 5, 2, -1, -5, 5, 10, 15, 30, 35, 40, 35, 30, 20, 15, 15, 12, 10, 10, 10, 10, 10, 10]

# Consumo base da casa (0.5 kWh a cada meia hora)
base_load = [0.5] * PERIODS

# Geração Solar (Amanhece no slot 12, gera um pouco)
solar = [0]*12 + [0.5, 1.0, 1.5, 2.0, 2.0, 1.5, 1.0, 0.5] + [0]*4

# --- O PROBLEMA MATEMÁTICO ---
# Queremos MINIMIZAR a função (custo)
prob = pulp.LpProblem("HomeEnergyOptimization", pulp.LpMinimize)

# Variáveis que o Solver vai decidir (kWh)
p_import = pulp.LpVariable.dicts("Import", range(PERIODS), lowBound=0)
p_export = pulp.LpVariable.dicts("Export", range(PERIODS), lowBound=0)
b_charge = pulp.LpVariable.dicts("Charge", range(PERIODS), lowBound=0, upBound=MAX_POWER*0.5)
b_discharge = pulp.LpVariable.dicts("Discharge", range(PERIODS), lowBound=0, upBound=MAX_POWER*0.5)
soc = pulp.LpVariable.dicts("SoC", range(PERIODS+1), lowBound=0, upBound=BATTERY_CAPACITY)

# Estado inicial da bateria
prob += soc[0] == INITIAL_SOC

# --- AS REGRAS DA FÍSICA (Restrições) ---
total_cost = 0
for i in range(PERIODS):
    # Regra 1: SoC atual = SoC anterior + Carga - Descarga
    prob += soc[i+1] == soc[i] + b_charge[i] - b_discharge[i]
    
    # Regra 2: Tudo que entra = Tudo que sai (Balanço de Energia)
    # Importação da Rede + Solar + Descarga da Bateria == Consumo da Casa + Exportação + Carga da Bateria
    prob += p_import[i] + solar[i] + b_discharge[i] == base_load[i] + p_export[i] + b_charge[i]
    
    # A Função Financeira: Custo = (Importação * Preço da Hora) - (Exportação * 15p fixo)
    total_cost += p_import[i] * prices[i] - p_export[i] * 15

prob += total_cost

# RESOLVE!
prob.solve(pulp.PULP_CBC_CMD(msg=False))

# --- MOSTRAR RESULTADOS ---
print(f"Status do Solver: {pulp.LpStatus[prob.status]}")
print(f"Custo Total Projetado: {pulp.value(prob.objective):.2f} pence\n")
print("Slot | Preco | Casa | Solar | Import | Export | Carga Bat | Descar Bat | SoC Final")
print("-" * 85)
for i in range(PERIODS):
    print(f"{i:02d}   | {prices[i]:5.1f} | {base_load[i]:4.1f} | {solar[i]:5.1f} | "
          f"{p_import[i].varValue:6.2f} | {p_export[i].varValue:6.2f} | "
          f"{b_charge[i].varValue:9.2f} | {b_discharge[i].varValue:10.2f} | {soc[i+1].varValue:9.2f}")
