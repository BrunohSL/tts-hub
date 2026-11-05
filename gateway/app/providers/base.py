"""Contrato de um provider de TTS.

Provider = texto → áudio NO FORMATO NATIVO dele (mp3 do edge, wav 22kHz do
Piper...). A conversão pro formato que o consumidor pediu (telefonia, mp3) é do
gateway (`audio.py`), uma vez só, igual pra todos. Provider novo só implementa
`synthesize` e declara as vozes.

Configuração que o painel muda (ordem, liga/desliga, timeout, voz padrão,
velocidade) vem do `config_store`; aqui ficam só os defaults de quando o
provider aparece pela primeira vez.
"""
import re
from typing import Optional


class ProviderError(Exception):
    """Falha do provider (rede, resposta vazia, erro do serviço)."""


class Provider:
    name: str = ""
    label: str = ""
    description: str = ""
    # Extensão do áudio que `synthesize` devolve (entrada do ffmpeg).
    native_format: str = "wav"

    # Apelidos genéricos → id da voz no provider. O consumidor pode mandar o
    # apelido ("feminina") ou o id cru do provider.
    voices: dict = {}
    default_voice: str = ""

    # Defaults de quando o provider é registrado pela 1ª vez no banco.
    default_enabled: bool = False
    default_timeout_s: float = 15.0
    default_cooldown_s: int = 60
    default_speed: int = 0

    # Sínteses simultâneas. None = sem limite (provider remoto/IO). Com limite, o
    # excedente espera numa fila com prioridade por consumidor (router.py).
    max_concurrent: Optional[int] = None

    # Regex de id de voz cru aceito além dos apelidos (None = só os apelidos).
    raw_voice_pattern: Optional[str] = None

    def resolve_voice(self, requested: Optional[str], configured: Optional[str]) -> str:
        """Pedido do consumidor > voz configurada no painel > default do provider.

        Apelido que este provider não tem (ex.: "feminina" no Piper, que só tem
        voz masculina) cai na voz configurada/default em vez de falhar: o áudio
        sai, com a voz que existe. O header X-TTS-Voice diz qual foi.
        """
        for candidate in (requested, configured, self.default_voice):
            if not candidate:
                continue
            if candidate in self.voices:
                return self.voices[candidate]
            if candidate in self.voices.values():
                return candidate
            if self.raw_voice_pattern and re.fullmatch(self.raw_voice_pattern, candidate):
                return candidate
        return next(iter(self.voices.values()))

    async def synthesize(self, text: str, voice: str, speed: int) -> bytes:
        """`speed` em % relativa ao normal (-10 = 10% mais devagar)."""
        raise NotImplementedError

    async def close(self) -> None:
        """Libera recursos (clientes HTTP) no shutdown."""

    def info(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "native_format": self.native_format,
            "voices": self.voices,
            "default_voice": self.default_voice,
            "max_concurrent": self.max_concurrent,
        }
