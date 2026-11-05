# Provider Piper do TTS Hub — texto → WAV nativo (22050Hz mono), 100% local.
#
# O modelo de voz é baixado NO BUILD e fica dentro da imagem: o container não
# depende de internet pra gerar áudio. É a diferença que importa em relação ao
# edge-tts, que depende de um serviço não oficial da Microsoft (18 dias fora em
# out/2026). Sem ffmpeg aqui: a conversão de formato é do gateway.
#
# Versões fixas de propósito.
FROM python:3.11.15-slim
WORKDIR /app
RUN pip install --no-cache-dir \
      piper-tts==1.8.0 \
      fastapi==0.135.1 \
      uvicorn==0.42.0
# Voz pt-BR masculina, qualidade média (~63MB). Outras pt-BR: cadu-medium,
# jeff-medium, edresson-low. Trocar = esta linha + PIPER_VOICE + voices em
# gateway/app/providers/piper.py.
RUN python -m piper.download_voices --download-dir /voices pt_BR-faber-medium
COPY app.py .
ENV PIPER_VOICE=/voices/pt_BR-faber-medium.onnx \
    PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
