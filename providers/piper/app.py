"""Provider Piper do TTS Hub — TTS local, sem internet.

Contrato INTERNO (só o gateway chama, pela rede do Swarm):
  POST /synthesize  { "text": "...", "length_scale": 1.11 }  → WAV nativo do modelo
                    (22050Hz mono). A conversão pra telefonia/mp3 é do gateway.
  GET  /health

Veio do `services/tts-piper` do Guardia (2026-11). Mudanças: devolve o WAV
nativo (sem ffmpeg aqui) e aceita a velocidade por request.
"""
import io
import logging
import os
import threading
import wave
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from piper import PiperVoice
from piper.config import SynthesisConfig
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ttshub.piper")

VOICE_PATH = os.getenv("PIPER_VOICE", "/voices/pt_BR-faber-medium.onnx")

# Carregado uma vez no boot (~63MB). Sem isso cada request pagaria a carga.
voice = PiperVoice.load(VOICE_PATH)

# Teto de sínteses simultâneas. Cada uma aloca ~190MB no ONNX: no teste, 5 em
# paralelo com 384M derrubaram o container (OOMKilled). Medido com 1 CPU
# (2026-11-03): teto 2 → mesmo throughput do teto 1 e pico de 671MB (vs 475MB).
# Com CPU limitada, paralelizar só gasta memória. A FILA (com prioridade por
# consumidor) fica no gateway, que manda no máximo PIPER_MAX_CONCURRENT por vez;
# este semáforo é só a trava de segurança. Os dois valores têm que bater.
MAX_CONCURRENT = int(os.getenv("PIPER_MAX_CONCURRENT", "1"))
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class SynthesizeIn(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    # Maior = mais devagar. O gateway converte a velocidade em % pra isto.
    length_scale: Optional[float] = Field(default=None, ge=0.5, le=2.0)


# `def` (não `async def`): a síntese é CPU pura e bloqueante; o FastAPI roda
# endpoints síncronos no threadpool, então o /health não trava.
@app.post("/synthesize")
def synthesize(body: SynthesizeIn):
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="texto vazio")
    # Só o tamanho: o texto tem nome e endereço de cliente.
    logger.info("gerando audio (%d caracteres)", len(body.text))
    config = SynthesisConfig(length_scale=body.length_scale) if body.length_scale else None
    buf = io.BytesIO()
    with _slots:
        with wave.open(buf, "wb") as wav_file:
            voice.synthesize_wav(body.text, wav_file, syn_config=config)
    return Response(content=buf.getvalue(), media_type="audio/wav")


@app.get("/health")
def health():
    return {"status": "ok"}
