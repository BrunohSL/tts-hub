"""Alertas no Discord. Nunca levanta exceção e nunca segura um request.

Disparados quando um provider cai (1 alerta por queda), quando volta, e quando
nenhum provider conseguiu gerar o áudio de um pedido. Mesmo canal do Guardia.

Envio confirmado ou nada (lição L-008 do Guardia: Discord mudo deixou o TTS 18
dias fora sem ninguém saber). O cooldown só conta a partir de um envio que o
Discord ACEITOU, e quem guarda estado de "já avisei" recebe um callback só no
sucesso. Falhou, a próxima falha do provider tenta de novo.

Nada de texto sintetizado no alerta: tem nome e endereço de cliente.
"""
import asyncio
import logging
import time
from typing import Callable, Optional

import httpx

from . import settings

logger = logging.getLogger("ttshub.alerts")

RED, GREEN, ORANGE = 0xE74C3C, 0x2ECC71, 0xE67E22

_last_sent: dict = {}   # chave → monotonic do último envio ACEITO
_inflight: set = set()  # chaves com envio em andamento (evita duplicar)
_tasks: set = set()


async def _post(embed: dict) -> bool:
    if not settings.DISCORD_WEBHOOK_URL:
        logger.warning("Alerta NÃO enviado: DISCORD_WEBHOOK_URL vazio (%s)", embed.get("title"))
        return False
    embed.setdefault("footer", {"text": f"tts-hub · {settings.INSTANCE}"})
    embed.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(settings.DISCORD_WEBHOOK_URL, json={"username": "TTS Hub", "embeds": [embed]})
        if res.status_code >= 300:
            logger.warning("Discord respondeu %s ao alerta (%s)", res.status_code, embed.get("title"))
            return False
        return True
    except Exception as err:
        logger.warning("Falha ao enviar alerta pro Discord (%s): %s", embed.get("title"), err)
        return False


def _fire(key: str, cooldown_s: int, embed: dict, on_sent: Optional[Callable[[], None]] = None) -> None:
    now = time.monotonic()
    if key in _inflight or now - _last_sent.get(key, -1e9) < cooldown_s:
        return
    _inflight.add(key)

    async def run():
        try:
            if await _post(embed):
                _last_sent[key] = time.monotonic()
                if on_sent:
                    on_sent()
        finally:
            _inflight.discard(key)

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def provider_down(name: str, label: str, reason: str, next_label: Optional[str], on_sent: Callable[[], None]) -> None:
    """Chamado a cada falha enquanto o provider está caído e a queda ainda não
    foi avisada; `on_sent` marca a queda como avisada. O cooldown por provider
    segura o flapping (cai/volta/cai)."""
    if next_label:
        title = f"TTS {label} caiu — usando {next_label}"
        description = f"Os áudios continuam saindo pelo {next_label}. Ver o painel do tts-hub e `docker service logs ttshub_gateway`."
        color = ORANGE
    else:
        title = f"TTS {label} caiu — SEM fallback disponível"
        description = "Nenhum outro provider saudável na cadeia: pedidos podem falhar. Ver o painel do tts-hub."
        color = RED
    _fire(f"down:{name}", settings.ALERT_COOLDOWN_S, {
        "title": title,
        "description": description,
        "color": color,
        "fields": [{"name": "Motivo", "value": reason[:1000] or "sem detalhe", "inline": False}],
    }, on_sent)


def provider_up(name: str, label: str, down_for_s: float) -> None:
    minutes = max(1, round(down_for_s / 60))
    _fire(f"up:{name}", 0, {
        "title": f"TTS {label} voltou",
        "description": f"Fora por ~{minutes} min. Voltou a atender na sua posição da cadeia.",
        "color": GREEN,
    })


def all_failed(consumer: str, attempts: list) -> None:
    fields = [
        {"name": a["provider"], "value": (a["error"] or "sem detalhe")[:1000], "inline": False}
        for a in attempts
    ] or [{"name": "Cadeia", "value": "nenhum provider habilitado", "inline": False}]
    _fire(f"all-failed:{consumer}", 600, {
        "title": f"Nenhum TTS gerou o áudio ({consumer})",
        "description": "Todos os providers permitidos falharam. O consumidor recebeu 503.",
        "color": RED,
        "fields": fields,
    })
