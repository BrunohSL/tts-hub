"""Conversão do áudio nativo do provider pro formato que o consumidor pediu.

Feita uma vez aqui (ffmpeg), igual pra todo provider: o provider novo não
precisa saber nada de telefonia nem de mp3.

Formato novo: incluir em FORMATS. O nome é o contrato da API (`format`).
"""
import asyncio
import os
import uuid

from . import settings

FORMATS = {
    # Telefonia: WAV PCM 16 bits, 8kHz, mono. É o que o discador do Guardia aceita.
    "wav_8k": {
        "ext": "wav",
        "media_type": "audio/wav",
        "args": ["-ar", "8000", "-ac", "1", "-c:a", "pcm_s16le"],
    },
    # MP3 mono 64kbps na taxa nativa do provider (edge 24kHz, Piper 22kHz).
    "mp3": {
        "ext": "mp3",
        "media_type": "audio/mpeg",
        "args": ["-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k"],
    },
}

FFMPEG_TIMEOUT_S = 30


class ConversionError(Exception):
    pass


async def convert(data: bytes, src_ext: str, fmt: str) -> tuple[bytes, str]:
    """Devolve (áudio convertido, media type).

    Arquivo de saída em vez de pipe: ffmpeg escrevendo WAV em stdout não volta
    pra preencher o tamanho no cabeçalho, e o discador pode recusar.
    """
    spec = FORMATS[fmt]
    os.makedirs(settings.WORK_DIR, exist_ok=True)
    uid = uuid.uuid4().hex
    src = os.path.join(settings.WORK_DIR, f"{uid}.in.{src_ext}")
    dst = os.path.join(settings.WORK_DIR, f"{uid}.out.{spec['ext']}")
    try:
        with open(src, "wb") as f:
            f.write(data)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", src, *spec["args"], dst,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise ConversionError("ffmpeg excedeu o tempo limite")
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            raise ConversionError(f"ffmpeg falhou: {stderr.decode(errors='replace')[:200]}")
        with open(dst, "rb") as f:
            out = f.read()
        if not out:
            raise ConversionError("ffmpeg gerou arquivo vazio")
        return out, spec["media_type"]
    finally:
        for path in (src, dst):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
