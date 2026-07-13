# HEM deploy runbook (Hetzner / Docker imutável)

Tudo é executado **no host Hetzner** como `root`, exceto onde dito. Working dir: `/srv/hem` (criar abaixo).

---

## 1. Pré-requisitos (instala Docker e cria estrutura)

```bash
apt-get update
apt-get install -y --no-install-recommends \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable --now docker

# User dedicado pra OpenClaw (uid 2000 — não reaproveita o uid 1000 órfão).
useradd -r -u 2000 -d /home/openclaw -m -s /usr/sbin/nologin openclaw

# Estrutura no host. /srv/hem é a única coisa que precisa ficar viva entre redeploys.
mkdir -p /srv/hem/data
```

## 2. Stage de volumes (copia o estado do checkout antigo)

Faça isso **antes** de parar o serviço antigo, pra ter um snapshot consistente do `data/`.

```bash
cp /root/home-energy-manager/.env /srv/hem/.env
chown root:1001 /srv/hem/.env
chmod 640 /srv/hem/.env

cp -a /root/home-energy-manager/data/. /srv/hem/data/
chown -R 1001:1001 /srv/hem/data
chmod 700 /srv/hem/data
```

Coloca `compose.yaml` (e o systemd unit) em `/srv/hem/`:

```bash
cp /root/home-energy-manager/deploy/compose.yaml          /srv/hem/compose.yaml
cp /root/home-energy-manager/deploy/hem.service           /etc/systemd/system/hem.service
cp /root/home-energy-manager/deploy/compose.daikin-auth.yaml /srv/hem/compose.daikin-auth.yaml
systemctl daemon-reload
```

`/srv/hem/.compose.env` (opcional, pra setar a interface Tailscale do host):
```bash
cat > /srv/hem/.compose.env <<'EOF'
HEM_IMAGE_TAG=main
HEM_TAILSCALE_IP=100.x.y.z
EOF
chmod 640 /srv/hem/.compose.env
```

## 3. Pull da imagem antes do cutover

```bash
# Login no GHCR (PAT com read:packages, ou GITHUB_TOKEN num CI helper).
echo "$GHCR_PAT" | docker login ghcr.io -u albinati --password-stdin

docker pull ghcr.io/albinati/home-energy-manager:main
docker images ghcr.io/albinati/home-energy-manager
```

## 4. Cutover (~2 min downtime)

```bash
# Para a unit antiga e desabilita.
systemctl stop home-energy-manager.service
systemctl disable home-energy-manager.service
mv /etc/systemd/system/home-energy-manager.service /etc/systemd/system/home-energy-manager.service.bak
systemctl daemon-reload

# Sobe o container.
systemctl enable --now hem.service
```

## 5. Smoke test (passa antes de continuar)

```bash
sleep 15  # boot inicial: lifespan + scheduler

curl -sS http://127.0.0.1:8000/api/v1/health | jq
# Esperado: {"status":"ok","version":"1.0.0","revision":"<sha>","mcp_token_present":true}

# MCP via HTTP precisa de bearer token.
TOKEN=$(cat /srv/hem/data/.openclaw-token)
curl -sS -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/mcp/ -X POST \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'
# Esperado: 81 (contagem verificada em 2026-07-13 via build_mcp().list_tools())

# Epic 13b/B1 — token da UI container é gerado no boot junto com o do OpenClaw.
ls -la /srv/hem/data/.hem-ui-token
# 0640 root:gid-do-uid1001 — pronto pra ser entregue à SPA container (B6).
# Sob HEM_UI_AUTH_REQUIRED=false (default), o /api/v1 segue aberto — só vira
# guard depois da cutover (B6).

# Bind correto (loopback + Tailscale, NÃO 0.0.0.0).
ss -lntp | grep ':8000'

# Heartbeat e cron rodando.
journalctl -u hem -f --since '1 min ago'
```

## 6. Cutover do OpenClaw (depois do smoke test passar)

```bash
# Para o openclaw-gateway atual rodando como root.
pkill -f openclaw-gateway || true

# Move o diretório e ajusta ownership.
mv /root/.openclaw /home/openclaw/.openclaw
chown -R openclaw:openclaw /home/openclaw

# Verifica que nada ficou com o uid 1000 órfão.
find /home/openclaw -not -user openclaw -ls   # deve ficar vazio

# Distribui o token pro openclaw.
install -m 0600 -o openclaw -g openclaw \
  /srv/hem/data/.openclaw-token /home/openclaw/.openclaw/hem-token

# Aponta o OpenClaw pro MCP HTTP. Edita o .env do openclaw (caminho exato pode variar):
#   HEM_MCP_URL=http://127.0.0.1:8000/mcp
#   HEM_MCP_TOKEN_FILE=/home/openclaw/.openclaw/hem-token
$EDITOR /home/openclaw/.openclaw/.env

# Reescreve o systemd unit pra rodar como user openclaw, sem docker group.
systemctl edit --full openclaw.service
# Adiciona/garante:
#   [Service]
#   User=openclaw
#   Group=openclaw
#   ProtectSystem=strict
#   ReadWritePaths=/home/openclaw
#   NoNewPrivileges=true

systemctl daemon-reload
systemctl enable --now openclaw.service

# Verifica.
ps -u openclaw -o pid,user,comm,args | grep openclaw
groups openclaw  # NÃO deve incluir 'docker'
```

Smoke do OpenClaw: pergunte ao agent "qual é meu SoC agora?" e confirme que ele responde via MCP HTTP.

## 7. Quarentena (mantém 1 semana antes de limpar)

Não rode o cleanup imediato. Deixe `/root/home-energy-manager/` e `home-energy-manager.service.bak` no lugar pelo menos 7 dias após cutover sem incidente. Isso garante rollback rápido se aparecer regressão.

## 7b. Deploys do dia-a-dia (pós-cutover): `rollout.sh`

Depois do cutover, o deploy padrão de uma nova imagem é UM comando no host:

```bash
# instala/atualiza o script (repo é a fonte da verdade):
scp deploy/rollout.sh root@<hem-host>:/srv/hem/rollout.sh && ssh root@<hem-host> chmod +x /srv/hem/rollout.sh

# deploy de um SHA já buildado pelo CI (docker-publish.yml verde para ESTE commit):
ssh root@<hem-host> /srv/hem/rollout.sh <full-git-sha>
```

O script encadeia as salvaguardas que antes eram manuais:
1. **manifest-guard** — nunca pinna uma tag impullável (pull em pipe sob `set -e`
   mascara falha → outage);
2. pin de `HEM_IMAGE_TAG` em `/srv/hem/.compose.env` (com `.bak`) + `systemctl restart hem`;
3. **health-verify** — espera `/api/v1/health` reportar a revisão NOVA; se não
   vier em ~3 min, **auto-rollback** para a tag anterior;
4. **prune** — remove imagens antigas do app mantendo exatamente a atual + a
   anterior (2026-07-02: deploys nunca removiam imagens de 534 MB; o disco
   cruzou 85% após uma sessão multi-PR). UI/quartz não são tocadas.

## 8. Rollback (caso algo dê errado)

```bash
systemctl stop hem.service
systemctl disable hem.service

mv /etc/systemd/system/home-energy-manager.service.bak /etc/systemd/system/home-energy-manager.service

# Devolve o data/ pro path antigo (atenção: ownership volta a root, e .openclaw-token sobra).
chown -R root:root /srv/hem/data
cp -a /srv/hem/data/. /root/home-energy-manager/data/

systemctl daemon-reload
systemctl start home-energy-manager.service

# OpenClaw também volta — restaura o launcher legado.
mv /home/openclaw/.openclaw /root/.openclaw
chown -R root:root /root/.openclaw
# Reaponta o .env do openclaw pro stdio launcher antigo.
```

## 9. Daikin OAuth re-enrollment (a cada 30 dias)

O `refresh_token` da Daikin expira a cada ~30 dias. Quando o heartbeat começar a logar 401 mesmo após refresh, rode:

```bash
# Do laptop, abre tunnel SSH pra publicar :8080 local.
ssh -L 8080:localhost:8080 root@<hem-host>.ts.net
# Então no host:
docker compose -f /srv/hem/compose.daikin-auth.yaml run --rm daikin-auth
# Abre a URL impressa no browser local; faz login Daikin; tokens caem em
# /srv/hem/data/.daikin-tokens.json. O container morre sozinho.

# Restart pra que o serviço pegue tokens novos.
systemctl restart hem.service
```

Para checar a idade dos tokens em qualquer momento:

```bash
docker exec hem python -c "
import json, datetime, time
d = json.load(open('/app/data/.daikin-tokens.json'))
print('obtained:', datetime.datetime.fromtimestamp(d['obtained_at']))
print('age days:', round((time.time() - d['obtained_at'])/86400, 1))
print('refresh expires (~30d):', datetime.datetime.fromtimestamp(d['obtained_at'] + 30*86400))
"
```

## 10. Cleanup pós-quarentena

Depois de 7+ dias rodando bem:

```bash
rm -rf /root/home-energy-manager
rm /etc/systemd/system/home-energy-manager.service.bak
systemctl daemon-reload
```

A partir desse ponto o código vive **só** na imagem em `ghcr.io/albinati/home-energy-manager` e no Git. OpenClaw, mesmo se for comprometido, não tem caminho de escrita pra alterar comportamento da próxima invocação.

## 11. SPA container (`hem-ui`) — ✅ CUTOVER CONCLUÍDO (histórico)

> **Este passo já foi executado.** O cutover do SPA terminou: o B5 **removeu
> toda a UI inline do container HEM** (não existe mais Jinja, `templates/` nem
> `static/` — a API só serve JSON), e `HEM_UI_AUTH_REQUIRED=true` já está no
> `.env`. Não há mais "UI legacy na :8000" pra comparar nem pra voltar.
> Mantido aqui só como registro do que foi feito.

Estado atual (o que você deve encontrar num host saudável):

- `hem-ui` roda como serviço no `compose.yaml`, nginx servindo o build Vite e
  fazendo reverse-proxy de `/api` → `hem`.
- SPA em Preact/TypeScript com **4 rotas** (`/`, `/insights`, `/report`,
  `/settings`) — o `wouter` resolve tudo client-side; o nginx faz
  `try_files $uri /index.html`.
- Publicado no tailnet via Tailscale funnel em `:8443` (TLS válido; é também o
  caminho que o sensor ESPHome usa — §12). `/mcp` **não** é exposto ali.
- O bearer vai pro browser via `/config.js` (escrito pelo `ui-entrypoint.sh` no
  boot do container) e é **viewer-only** — o `HEM_ADMIN_TOKEN` nunca vai pro
  browser.

```bash
# Deploy de uma nova versão da SPA (sem derrubar o loop de controle):
docker manifest inspect ghcr.io/albinati/home-energy-manager-ui:sha-<sha> >/dev/null
sed -i "s|^HEM_UI_IMAGE_TAG=.*|HEM_UI_IMAGE_TAG=sha-<sha>|" /srv/hem/.compose.env
cd /srv/hem && docker compose up -d --no-deps hem-ui

# Smoke test
curl -sS http://127.0.0.1:8080/healthz          # "ok"
curl -sS http://127.0.0.1:8000/api/v1/health    # health segue público
```

## 12. Ingestão do sensor ESPHome de temperatura interna (#540 W1)

O HEM aceita leituras de temperatura interna em `POST /api/v1/sensors/indoor`
(batch, idempotente em `(captured_at, room)`). Um sensor ESPHome **na LAN de
casa** alcança o HEM **no Hetzner** reusando o **funnel Tailscale do `hem-ui`
(`:8443`)**, que já expõe `/api/` publicamente com TLS válido (e **não** expõe
`/mcp`). Nenhum proxy/porta/funnel novo — só o token **escopado**
`HEM_SENSOR_INGEST_TOKEN`, que destrava **apenas** essa rota (nunca o admin).

O que protege, em camadas: o funnel termina TLS (bearer nunca trafega em claro);
`ApiV1RoleAuth` deixa o token de ingestão satisfazer **só** um write em
`/api/v1/sensors/indoor` — 401 em qualquer outra escrita e em toda leitura
admin (Settings/Journal). Um vazamento do firmware só consegue postar
temperatura falsa nessa rota; rotacione o token pra revogar o device.

```bash
# 1. Gera o token escopado e adiciona no .env do HEM.
INGEST_TOK=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
echo "HEM_SENSOR_INGEST_TOKEN=$INGEST_TOK" >> /srv/hem/.env
echo "Token do sensor (guarda pro YAML do ESPHome): $INGEST_TOK"

# 2. Restart do hem pra ler o token novo do .env (nada mais muda no deploy).
systemctl restart hem

# 3. Smoke test pelo funnel que JÁ existe (do teu laptop, mesmo fora do tailnet).
#    O payload pode carregar tudo que o sensor mede — temp alimenta o modelo
#    térmico; mac/humidade/pressão/extras vão pro log lossless por-device (W1c).
curl -sS -X POST https://<host>.ts.net:8443/api/v1/sensors/indoor \
  -H "Authorization: Bearer $INGEST_TOK" -H "Content-Type: application/json" \
  -d '{"readings":[{"captured_at":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'","temp_c":21.3,"humidity_pct":55.0,"pressure_hpa":1013.2,"room":"sala","mac":"70:4B:CA:26:EA:B4","device_id":"hem-temp-sensor"}]}'
# Esperado: {"received":1,"written":1,"logged":1}

# 3b. Vê o que foi logado por-device:
curl -sS "https://<host>.ts.net:8443/api/v1/sensors/devices"           # 1 linha/device
curl -sS "https://<host>.ts.net:8443/api/v1/sensors/device-log?hours=1"  # linhas cruas c/ payload

# 4. Confere que o token NÃO destrava mais nada (escopo):
curl -s -o /dev/null -w "%{http_code}\n" https://<host>.ts.net:8443/api/v1/settings \
  -H "Authorization: Bearer $INGEST_TOK"                       # → 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  https://<host>.ts.net:8443/api/v1/optimization/propose \
  -H "Authorization: Bearer $INGEST_TOK" -d '{}'               # → 401
```

**Campos aceitos por leitura** (`readings[]`): `captured_at` (obrigatório, UTC),
`temp_c` (opcional; só entra no modelo térmico se −20..45), `humidity_pct`,
`pressure_hpa`, `room` (único por nó — chave de storage), `source`, `device_id`,
`mac`, + **qualquer campo extra** (preservado no `payload_json` do log). Nada que
o sensor manda é descartado.

**Cadência de envio (recomendação 2026-07-12): a cada 10 min.** Os
consumidores não usam mais resolução que isso: o LP/guard de conforto só exigem
leitura mais fresca que `INDOOR_SENSOR_STALE_MINUTES=30` (10 min ⇒ 3 leituras na
janela, tolera 2 perdas de Wi-Fi), o learner W2 reamostra em bins de **30 min**
(gap >45 min quebra o episódio de decaimento — não passe de 15 min de intervalo)
e o rollup WARM é de 15 min. Amostre o sensor localmente a cada 30–60 s com um
filtro (`median`/`sliding_window_moving_average`) e reporte o valor filtrado —
sinal limpo melhora o fit de τ mais que frequência alta. **Mantenha a MESMA
cadência em todos os nós**: cadências mistas fazem a composição do house-mean
oscilar entre bins do W2 (ver comentário em
`src/analytics/thermal_learning.py:_resample_house_mean`).

**Config do ESPHome** (use o `http_request` + `time` pra carimbar UTC). O
`captured_at` precisa ser ISO-8601 **UTC** (`...Z`):

```yaml
# secrets.yaml → hem_ingest_token: "<o token do passo 1>"
time:
  - platform: sntp
    id: sntp_time

http_request:
  useragent: esphome-hem-sensor
  timeout: 10s
  verify_ssl: true      # funnel tem cert válido — mantenha ligado

# Cabeçalho via substitutions (texto substituído em compile-time — evita o
# gotcha de !secret dentro de !lambda). Muda `room`/`node_name` por nó.
substitutions:
  node_name: hem-temp-sensor-sala
  room: sala
  ingest_token: "<o token do passo 1>"

interval:
  - interval: 600s   # 10 min — ver "Cadência de envio" acima; igual em todos os nós
    then:
      - if:
          condition:
            lambda: 'return id(sntp_time).now().is_valid() && !isnan(id(temp_aht20).state);'
          then:
            - http_request.post:
                url: https://<host>.ts.net:8443/api/v1/sensors/indoor
                headers:
                  Content-Type: application/json
                  Authorization: "Bearer ${ingest_token}"
                body: !lambda |-
                  char ts[24];
                  time_t now = id(sntp_time).now().timestamp;
                  strftime(ts, sizeof(ts), "%Y-%m-%dT%H:%M:%SZ", gmtime(&now));
                  char buf[256];
                  snprintf(buf, sizeof(buf),
                    "{\"readings\":[{\"captured_at\":\"%s\",\"temp_c\":%.2f,"
                    "\"humidity_pct\":%.1f,\"pressure_hpa\":%.1f,"
                    "\"room\":\"${room}\",\"device_id\":\"${node_name}\",\"mac\":\"%s\"}]}",
                    ts, id(temp_aht20).state, id(hum_aht20).state,
                    id(press_bmp280).state, get_mac_address().c_str());
                  return std::string(buf);
```

> Troque `temp_aht20`/`hum_aht20`/`press_bmp280` pelos `id`s dos sensores de
> vocês. `temp_c` vem do **AHT20**. Campos extras (2ª temperatura, RSSI…) podem
> ser adicionados ao JSON — o HEM preserva todos no log por-device e a UI mostra
> como hint no Journal → Sensors. Vale adicionar o sensor `wifi_signal` do
> ESPHome ao payload: distingue "Wi-Fi fraco" de "sensor travado" ao diagnosticar
> gaps de leitura.

Rollback / desligar: `sed -i '/^HEM_SENSOR_INGEST_TOKEN=/d' /srv/hem/.env &&
systemctl restart hem` — sem o token, a rota volta a ser admin-only e o sensor
passa a levar 401 (nada mais no deploy muda). Revogar/rotacionar um device:
troca o valor do `HEM_SENSOR_INGEST_TOKEN` + restart + atualiza o YAML.

## Troubleshooting

| Sintoma | Causa provável | Ação |
|---|---|---|
| `/api/v1/health` retorna `mcp_token_present: false` | Volume `data/` não montou ou está read-only | `docker exec hem ls -la /app/data && docker inspect hem \| jq '.[0].Mounts'` |
| `/mcp/` retorna 503 "service token not provisioned" | Lifespan ainda não rodou ou bootstrap falhou | `journalctl -u hem -e \| grep -i token`, verifica permissão de escrita em `/srv/hem/data` |
| OpenClaw retorna `Connection closed` | Token errado, ou OpenClaw apontando pro stdio antigo | `cat /home/openclaw/.openclaw/hem-token` deve bater com `cat /srv/hem/data/.openclaw-token` |
| LP solver Infeasible recorrente | Problema de modelagem (não migração) | Ver `project_lp_infeasibles` no contexto — fora deste escopo |
| Container reinicia em loop | OOM (mem_limit 400m) ou erro no startup | `docker logs hem --tail 100`, considera elevar `mem_limit` |
| Daikin 429 daily-limit | Quota 200 req/dia esgotada | **`DAIKIN_HTTP_429_MAX_RETRIES=0` PRECISA estar no `.env`** — o default no código é `3` (`src/config.py`), e o Daikin manda `Retry-After: ~86400` quando estoura o limite diário, então um cliente que faz retry trava horas no startup. Depois disso, espera 24 h ou ajusta `HEARTBEAT_INTERVAL_SECONDS` |
