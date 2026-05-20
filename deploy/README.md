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
# Esperado: 80 (76 originais + 3 audit tools do Epic 13a + lp_scorecard)

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

## 11. Cutover do SPA container (Epic 13b / B6)

A partir do PR #356 (B1) o token UI já é gerado no boot do HEM em
`/srv/hem/data/.hem-ui-token`. O B6 só liga o container `hem-ui` no
compose e (uma vez que a SPA estiver verificada) flipa o gate flag.

```bash
# 1. Garante que o image SPA está publicado (após B3/B4 baterem em main).
docker pull ghcr.io/albinati/home-energy-manager-ui:main

# 2. Sobe o serviço hem-ui (já está em compose.yaml; o pull_policy=always
#    cuida do refresh). HEM continua servindo a UI legacy em paralelo.
cd /srv/hem && docker compose up -d hem-ui

# 3. Smoke test do SPA na porta 8080 (loopback + Tailnet).
curl -sS http://127.0.0.1:8080/healthz
# Esperado: "ok"

# 4. Abrir http://openclaw-overbot.tail0dbf20.ts.net:8080/ no navegador.
#    Cockpit, history, forecast, insights, workbench, settings devem
#    funcionar idênticos à UI inline (que segue rodando na :8000).
#    Inspect Network: as chamadas /api/v1/* devem ter Authorization: Bearer.

# 5. Quando confiar que está OK, flipa o gate flag pra exigir bearer em
#    /api/v1/* — depois disso a UI inline em :8000 NÃO funciona mais
#    (não envia bearer). Só faz esse passo depois de verificar :8080.
echo 'HEM_UI_AUTH_REQUIRED=true' >> /srv/hem/.env
chmod 640 /srv/hem/.env   # perms importam — ver feedback_flag_before_env_overwrite
systemctl restart hem
sleep 8
curl -sS http://127.0.0.1:8000/api/v1/health   # health stays public
# A UI legacy em :8000 vai responder 401 sem header — esperado.
# B5 remove ela do container HEM no PR seguinte.

# 6. Rollback (se o SPA tiver bug): tira o flag + restart, UI legacy volta.
sed -i '/^HEM_UI_AUTH_REQUIRED=/d' /srv/hem/.env
systemctl restart hem
```

## Troubleshooting

| Sintoma | Causa provável | Ação |
|---|---|---|
| `/api/v1/health` retorna `mcp_token_present: false` | Volume `data/` não montou ou está read-only | `docker exec hem ls -la /app/data && docker inspect hem \| jq '.[0].Mounts'` |
| `/mcp/` retorna 503 "service token not provisioned" | Lifespan ainda não rodou ou bootstrap falhou | `journalctl -u hem -e \| grep -i token`, verifica permissão de escrita em `/srv/hem/data` |
| OpenClaw retorna `Connection closed` | Token errado, ou OpenClaw apontando pro stdio antigo | `cat /home/openclaw/.openclaw/hem-token` deve bater com `cat /srv/hem/data/.openclaw-token` |
| LP solver Infeasible recorrente | Problema de modelagem (não migração) | Ver `project_lp_infeasibles` no contexto — fora deste escopo |
| Container reinicia em loop | OOM (mem_limit 400m) ou erro no startup | `docker logs hem --tail 100`, considera elevar `mem_limit` |
| Daikin 429 daily-limit | Quota 200 req/dia esgotada | `DAIKIN_HTTP_429_MAX_RETRIES=0` em `.env` (já é o default) — espera 24h ou ajusta `HEARTBEAT_INTERVAL_SECONDS` |
