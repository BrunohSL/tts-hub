# Gateway do TTS Hub: API, painel, cadeia de fallback e o provider edge-tts
# (in-process). O Piper roda em container próprio (providers/piper).
#
# Único não fixado: o ffmpeg vem do apt do Debian e acompanha a base.
FROM python:3.11.15-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV DATA_DIR=/data \
    WORK_DIR=/tmp/ttshub \
    PYTHONUNBUFFERED=1
EXPOSE 8000
# start-first no Swarm espera o container novo ficar healthy antes de matar o velho.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"
# 1 worker de propósito: circuit breaker, fila e probes vivem em memória.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*"]
