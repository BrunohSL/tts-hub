# TTS Hub

Texto → áudio para todos os produtos (Guardia, produto da câmera, os próximos).
O consumidor manda o texto e o formato; o hub escolhe o provider, faz o fallback,
converte o áudio, alerta quando algo cai e mostra tudo num painel ADM.

Nenhum consumidor sabe qual TTS existe por trás: trocar o principal, incluir um
provider novo ou ajustar timeout é no painel (ou num arquivo deste repo), sem
deploy de consumidor.

```
 Guardia ─┐                       ┌─ edge   (in-process, internet)
 câmera  ─┼─▶ ttshub_gateway ─────┤
 ...     ─┘   API + painel +      └─ piper  (ttshub_piper, local)
              fallback + alertas
```

| serviço | o que é | por que separado |
|---|---|---|
| `ttshub_gateway` | FastAPI: API, painel, cadeia de fallback, circuit breaker, fila, probes, alertas, ffmpeg. O **edge-tts roda aqui dentro** (é só I/O de rede). | — |
| `ttshub_piper` | Piper (TTS local, sem internet), contrato interno | Cada síntese aloca ~190MB; um OOM dele não pode derrubar o gateway |

Veio do `services/tts` + `services/tts-piper` do Guardia (nov/2026).

---

## API dos consumidores

Autenticação: `Authorization: Bearer tth_...` (ou `X-API-Key`). Uma chave por
consumidor, criada no painel.

### `POST /v1/synthesize`

```json
{ "text": "Olá Maria, ...", "format": "wav_8k", "provider": "auto", "voice": "feminina", "speed": -10 }
```

| campo | obrigatório | padrão | |
|---|---|---|---|
| `text` | sim | — | até 3000 caracteres |
| `format` | sim | — | `wav_8k` (telefonia: PCM 16 bits, 8kHz, mono) ou `mp3` (mono, 64kbps, taxa nativa) |
| `provider` | não | `auto` | `auto` = cadeia com fallback · `primary` = só o principal, sem fallback · `<nome>` = só aquele |
| `voice` | não | do painel | apelido (`feminina`, `masculina`) ou id do provider. Se o provider não tiver a voz pedida, usa a padrão dele (o Piper só tem masculina) |
| `speed` | não | do painel | % relativa ao normal, de -50 a 50 |

**200** devolve o áudio no corpo (`audio/wav` ou `audio/mpeg`), com estes headers:

| header | |
|---|---|
| `X-TTS-Provider` | quem gerou (`edge`, `piper`) |
| `X-TTS-Voice` | id da voz usada |
| `X-TTS-Fallback` | `true` se não foi o principal da cadeia |
| `X-TTS-Duration-Ms` | tempo total, incluindo fila e tentativas que falharam |

**Erros:**
- 400: provider desconhecido.
- 401: sem chave.
- 409: provider pedido por nome está desabilitado.
- 422: validação.
- 503: nenhum provider gerou o áudio. Corpo: `{ "error", "attempts": [{ "provider", "error" }] }`.

**Timeout do consumidor:** some os timeouts da cadeia, com folga. Com a config
padrão (edge 10s + piper 90s), use ~110s em `auto`.

### `GET /v1/providers`

Cadeia atual e saúde de cada provider, mais os formatos aceitos.

---

## Como a cadeia funciona

- **Ordem** = a do painel. O 1º habilitado é o principal e os seguintes são o
  fallback, na ordem. Mudar vale na hora. O painel não deixa desabilitar o último.
- **Circuit breaker:** quando um provider falha, ele vai pro fim da cadeia por
  `cooldown_s` (padrão 60s). Continua sendo tentado se todos os outros falharem.
  Sem isso, com o edge caído, cada ligação esperaria 10s de timeout antes do
  Piper. Ele volta pra posição dele no primeiro sucesso (pedido real depois do
  cooldown, ou probe).
- **Probes:** a cada 5 min cada provider habilitado sintetiza uma frase de teste
  (a cada 30s enquanto estiver caído). É isso que descobre o fallback quebrado
  antes de alguém precisar dele, e o que detecta a volta.
- **Fila com prioridade:** provider com limite de concorrência (o Piper, 1 por
  vez) enfileira o excedente pela **prioridade da API key**, maior primeiro.
  Assim o volume da câmera não atrasa a ligação de alarme do Guardia. A espera
  conta no timeout do provider. Estourar o timeout ainda na fila **não** derruba
  o provider: ele está ocupado, não quebrado.
- **Conversão:** o provider devolve o áudio nativo e o gateway converte (ffmpeg)
  pro `format` pedido, igual pra todos. Arquivo de saída, não pipe: WAV em pipe
  sai com o cabeçalho sem tamanho, e o discador pode recusar.

Estado (breaker, fila, contadores) em memória: **1 réplica, 1 worker**. Restart
zera os contadores e o estado; a config do painel fica no SQLite (`ttshub_data`).

## Alertas (Discord)

Um alerta por queda, e só conta como avisado quando o Discord aceita: se o envio falhar, a próxima falha do provider tenta de novo (lição L-008 do Guardia). Cooldown de 15 min por provider contra cai/volta/cai:

| alerta | quando |
|---|---|
| 🟠 `TTS <x> caiu — usando <y>` | a primeira falha de um provider saudável (pedido real ou probe) |
| 🔴 `TTS <x> caiu — SEM fallback` | idem, quando não sobrou nenhum outro saudável |
| 🟢 `TTS <x> voltou` | o primeiro sucesso depois de uma queda que foi avisada |
| 🔴 `Nenhum TTS gerou o áudio (<consumidor>)` | um pedido que esgotou a cadeia (cooldown de 10 min por consumidor) |

O teste do painel não alerta: quem testou já está vendo o erro. Nenhum alerta nem
log tem o texto sintetizado, porque ele tem nome e endereço de cliente. Logs têm
só o tamanho.

## Painel ADM

`https://tts-hub.6lab.com.br/admin`. Tem: status de cada provider, ordem e
liga/desliga, timeout/cooldown/velocidade/voz, "testar agora" (probe), teste de
voz (texto → toca no navegador, qualquer provider ou formato), API keys e
auditoria (quem mudou o quê).

**Login:** conta ADM do Guardia. O gateway pergunta ao Guardia
(`POST /api/internal/auth/verify`, header `X-Service-Key`) se email e senha
valem, e qual é o papel. Só `ADMIN_ROLES` entram. Nenhum token do Guardia é
emitido nem guardado: o painel tem sessão própria (cookie assinado, 12h).

**Acesso local de emergência:** usuário `admin` com a senha do secret
`ttshub_admin_local_password`. Serve pra entrar com o Guardia fora do ar e pra
primeira subida, antes de existir a chave do Guardia.

---

## Provider novo

1. Criar `gateway/app/providers/<nome>.py`, herdando de `Provider`: implementa
   `synthesize(text, voice, speed) -> bytes` (áudio nativo), declara
   `native_format`, `voices`, `default_voice` e os defaults.
2. Registrar em `gateway/app/providers/__init__.py`.
3. Deploy. Ele aparece no fim da cadeia, habilitado ou não conforme
   `default_enabled`. Testar no painel antes de subir de posição.

API paga (Azure oficial, ElevenLabs, Google, Polly) entra como provider dentro
do gateway, com chamada HTTP. Container próprio só para modelo local pesado,
como o Piper.

**Formato novo:** incluir em `gateway/app/audio.py` (`FORMATS`). Toda a cadeia
passa a aceitar na hora.

---

## Operação

### edge-tts voltou a dar 403 / NoAudioReceived

É a Microsoft recusando a versão da lib (out/2026: 18 dias sem ligação). Subir
`edge-tts` em `gateway/requirements.txt` → push → testar no painel e com uma
ligação real. Enquanto isso o Piper atende sozinho, e o alerta já avisou.

### Medidas do Piper (2026-11-03, 1 CPU)

| | |
|---|---|
| Ocioso | 156MB |
| 1 síntese (~130 caracteres, 8s de áudio) | ~3,6s, pico 340MB |
| 10 pedidos simultâneos | ~43s o último, pico 475MB |

Teto de 1 síntese por vez (`PIPER_MAX_CONCURRENT`, nos **dois** serviços da
stack). Com 2 em paralelo o tempo total é o mesmo e o pico vai a 671MB. Com 5,
deu OOM.

### Métricas

`GET /metrics` (Bearer `ttshub_metrics_token`): `ttshub_requests_total`,
`ttshub_fallback_total`, `ttshub_failed_total`, `ttshub_synthesis_seconds`,
`ttshub_provider_up`, `ttshub_queue_waiting`.

---

## Primeira subida

No servidor (`guardia-novo`). A função `criar_secret` está no README do repo
`stacks`.

```bash
# 1. Secrets
openssl rand -hex 32 | tr -d '\n' | docker secret create ttshub_session_secret -
openssl rand -hex 32 | tr -d '\n' | docker secret create ttshub_metrics_token -
# chave que o Guardia exige do hub no login do painel: o Guardia monta o MESMO secret
openssl rand -hex 32 | tr -d '\n' | docker secret create ttshub_guardia_service_key -
criar_secret ttshub_admin_local_password
# mesmo canal de alerta do Guardia
CID=$(docker ps -q -f name=guardia-api_guardia-api)
docker exec $CID cat /run/secrets/guardia_discord_webhook_url | tr -d '\n' | docker secret create ttshub_discord_webhook_url -

# 2. DNS: A tts-hub.6lab.com.br → IP do servidor (coberto pelo wildcard *.6lab.com.br)

# 3. Código e deploy
git clone git@github.com:<org>/tts-hub.git /opt/apps/tts-hub
cd /opt/apps/tts-hub && bash deploy.sh

# 4. Conferir
docker service ls | grep ttshub
docker service logs ttshub_gateway --since 5m
```

Depois:
1. Entrar no painel com `admin` + a senha local.
2. Criar a chave `guardia` com **prioridade 100**.
3. Testar a voz.
4. Ligar o Guardia no hub (lado do Guardia: `TTS_URL` + a chave).

Deploy automático: push na `main` → `.github/workflows/deploy.yml` → SSH →
`deploy.sh`. Precisa dos secrets `SSH_HOST`, `SSH_USER` e `SSH_KEY` no GitHub.

**Diferente do Guardia:** o `deploy.sh` roda `docker stack deploy`, então env,
secret e limite **do arquivo** sobem a cada push. Ajuste manual com
`service update --env-add` é desfeito no próximo deploy: mude o
`docker-stack.yml`.

## Desenvolvimento

```bash
docker compose up --build      # http://localhost:8000/admin  —  admin / dev
```

Sem Discord nem Guardia (alerta vira log). O SQLite fica em `./data` (no
`.gitignore`). O edge precisa de internet.
