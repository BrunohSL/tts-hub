"""Piper: TTS local (sem internet), em container próprio (`providers/piper`).

Fica fora do processo do gateway por causa da memória: cada síntese aloca
~190MB no ONNX e o container já morreu por OOM no teste. Isolado, um OOM do
Piper não derruba o gateway, o edge nem o painel.

Voz mais robótica que a do edge; é o fallback.
"""
import httpx

from .. import settings
from .base import Provider, ProviderError


class PiperProvider(Provider):
    name = "piper"
    label = "Piper"
    description = "Local, sem internet (container ttshub_piper). Voz mais robótica."
    native_format = "wav"

    # Só uma voz pt-BR na imagem (masculina). Trocar = Dockerfile do piper.
    voices = {"masculina": "pt_BR-faber-medium"}
    default_voice = "masculina"

    default_enabled = True
    # Sintetiza 1 por vez (~3,6s cada, 1 CPU). O timeout inclui a espera na fila:
    # 10 pedidos simultâneos, o último sai em ~43s.
    default_timeout_s = 90.0
    default_speed = -10
    max_concurrent = settings.PIPER_MAX_CONCURRENT

    def __init__(self):
        # Timeout aqui é só teto de segurança: quem manda é o asyncio.wait_for do
        # router, com o timeout configurado no painel.
        self._client = httpx.AsyncClient(base_url=settings.PIPER_URL, timeout=300.0)

    async def synthesize(self, text: str, voice: str, speed: int) -> bytes:
        # Velocidade em % → length_scale do Piper (maior = mais devagar).
        # -10% → 1/0,9 ≈ 1,11, o mesmo 1,1 que o fallback usava no Guardia.
        length_scale = round(1 / max(0.5, 1 + speed / 100), 3)
        try:
            res = await self._client.post("/synthesize", json={"text": text, "length_scale": length_scale})
        except httpx.HTTPError as err:
            raise ProviderError(f"piper inacessível: {err.__class__.__name__}: {err}") from err
        if res.status_code != 200:
            raise ProviderError(f"piper HTTP {res.status_code}: {res.text[:200]}")
        if not res.content:
            raise ProviderError("piper devolveu áudio vazio")
        return res.content

    async def close(self) -> None:
        await self._client.aclose()
