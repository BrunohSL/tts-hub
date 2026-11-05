# TTS Hub

Gateway de texto→áudio compartilhado entre os produtos (Guardia, produto da câmera...).
Arquitetura, contrato da API, operação e deploy: **README.md** (ler antes de mexer).

## Stack

- Python 3.11 + FastAPI + uvicorn, ffmpeg. SQLite (config do painel) em volume.
- `gateway/` — API, painel (`app/static`, JS puro sem build), cadeia/fallback (`router.py`), providers in-process (`providers/`).
- `providers/piper/` — container próprio (memória). Contrato interno: `POST /synthesize` → WAV nativo.

## Invariantes

- **1 réplica, 1 worker.** Circuit breaker, fila com prioridade e probes vivem em memória.
- **Nunca logar nem alertar o texto sintetizado** — tem nome e endereço de cliente. Só o tamanho.
- **Provider devolve áudio nativo; conversão é só no `audio.py`.** WAV sempre por arquivo, nunca pipe (cabeçalho sem tamanho, discador recusa).
- **Erro do consumidor não pode abrir o breaker** (ex.: voz inválida → cai na voz padrão, não falha o provider). O breaker é global: abrir por erro de um consumidor tira o provider de todos.
- **Timeout na fila ≠ falha do provider** (`QueueTimeout` não abre breaker).
- **Versões fixas** (requirements/Dockerfile). edge-tts é cliente não oficial; subir é mudança deliberada com teste de ligação real.
- `PIPER_MAX_CONCURRENT` igual nos dois serviços da stack.
- Stack `ttshub` (nunca `tts`, que é a stack antiga do Guardia). Hostnames `ttshub_gateway` / `ttshub_piper` são contrato com os consumidores.

## Validação

`docker compose up --build` e exercitar com curl/painel (README, "Desenvolvimento"). Sem suíte de testes por enquanto.

## Rules of engagement

- **NUNCA rodar `git commit`, `git add` ou `git push`.** Git é do usuário; deixar mudanças no working tree.
- O `deploy.sh` aplica o `docker-stack.yml` inteiro a cada push na `main` — mudança de env/secret vai junto. Secret novo precisa ser criado no servidor ANTES do push.
- Não introduzir abstração além do pedido.
