"""edge-tts: TTS do Microsoft Edge por um cliente NÃO OFICIAL.

Voz natural e de graça, mas quebra quando a Microsoft mexe no endpoint: em
out/2026 a 7.2.7 passou a levar 403 e nenhuma ligação do Guardia saiu por 18
dias. A correção nesses casos costuma ser subir a versão do `edge-tts` no
requirements.txt (e testar uma ligação real depois).

Roda dentro do processo do gateway: é só I/O de rede, leve. Precisa de saída
para a internet.
"""
import edge_tts

from .base import Provider, ProviderError


class EdgeProvider(Provider):
    name = "edge"
    label = "Edge TTS"
    description = "Microsoft Edge (cliente não oficial). Voz natural, depende de internet."
    native_format = "mp3"

    voices = {
        "feminina": "pt-BR-FranciscaNeural",
        "masculina": "pt-BR-AntonioNeural",
    }
    default_voice = "feminina"
    # Sem id cru (raw_voice_pattern): voz que o Edge não conhece faz ele falhar,
    # e isso abriria o circuit breaker PRA TODOS por erro de um consumidor. Voz
    # nova = incluir no dict acima, testada.

    default_enabled = True
    # Responde em 1-2s. Timeout curto: travado (em vez de falhando) atrasaria
    # cada ligação até cair no fallback.
    default_timeout_s = 10.0
    default_speed = -10  # ouvido por telefone, um pouco mais devagar

    async def synthesize(self, text: str, voice: str, speed: int) -> bytes:
        communicate = edge_tts.Communicate(text, voice=voice, rate=f"{int(speed):+d}%")
        chunks = []
        try:
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    chunks.append(chunk["data"])
        except Exception as err:  # a lib levanta tipos variados (aiohttp, 403...)
            raise ProviderError(f"edge-tts: {err.__class__.__name__}: {err}") from err
        audio = b"".join(chunks)
        if not audio:
            raise ProviderError("edge-tts devolveu áudio vazio")
        return audio
