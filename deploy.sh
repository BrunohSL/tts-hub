#!/bin/bash
# Deploy do TTS Hub em produção. Roda no servidor, em /opt/apps/tts-hub
# (a GitHub Action chama este script a cada push na main).
#
# Diferente do Guardia, aqui o docker-stack.yml é a fonte da verdade: o
# `stack deploy` aplica env/secrets/limites do arquivo a cada deploy. Ajuste
# manual com `docker service update --env-add` é DESFEITO no próximo push —
# mude o arquivo.
set -euo pipefail
cd "$(dirname "$0")"

STACK=ttshub

echo "🚀 Deploy do TTS Hub"

echo "📥 Git pull..."
git pull origin main

image_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null || true; }

# Gateway: sem cache, pra pegar correção de segurança da base a cada deploy.
OLD_GW=$(image_id tts-hub-gateway:latest)
echo "🔨 Build do gateway..."
docker build --no-cache --pull -t tts-hub-gateway:latest gateway/

# Piper: COM cache. Sem cache rebaixaria o modelo de voz (~63MB) a cada push.
OLD_PIPER=$(image_id tts-hub-piper:latest)
echo "🔨 Build do piper..."
docker build --pull -t tts-hub-piper:latest providers/piper/

echo "📦 Aplicando a stack..."
docker stack deploy --resolve-image never -c docker-stack.yml "$STACK"

# Mesma tag (:latest) não reinicia o serviço sozinha: força quando a imagem mudou.
if [ "$(image_id tts-hub-gateway:latest)" != "$OLD_GW" ]; then
  echo "⚡ Atualizando gateway..."
  docker service update --force --detach=false --image tts-hub-gateway:latest "${STACK}_gateway"
fi
if [ "$(image_id tts-hub-piper:latest)" != "$OLD_PIPER" ]; then
  echo "⚡ Atualizando piper..."
  docker service update --force --detach=false --image tts-hub-piper:latest "${STACK}_piper"
fi

echo ""
echo "✅ Deploy concluído"
echo "📝 Logs: docker service logs -f ${STACK}_gateway"
