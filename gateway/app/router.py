"""Cadeia de providers: escolha, fallback, circuit breaker, fila com prioridade e probes.

Cadeia = providers HABILITADOS na ordem do painel. O 1º é o principal.

`selection` de um pedido:
  auto     — percorre a cadeia; o primeiro que gerar o áudio responde.
  primary  — só o 1º da cadeia, sem fallback (prévia: ouvir a voz real).
  <nome>   — só aquele provider, sem fallback.

Circuit breaker: falhou → o provider fica `cooldown_s` fora da frente da cadeia
(vai pro fim: ainda é tentado se todos os outros falharem). Sem isso, com o edge
caído cada ligação esperaria os 10s do timeout antes de cair no Piper. A volta é
por sucesso: de um pedido real depois do cooldown ou do probe em background.

Fila: provider com `max_concurrent` (Piper) atende N por vez; o resto espera
numa fila ordenada pela prioridade da API key (maior primeiro). É o que impede
que o volume de um consumidor atrase a ligação de alarme do Guardia. A espera
conta no timeout do provider; estourar o timeout AINDA NA FILA não derruba o
provider (ele está ocupado, não quebrado).

Estado em memória: o gateway roda com 1 réplica e 1 worker.
"""
import asyncio
import heapq
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from . import alerts, audio, metrics, settings
from .config_store import ConfigStore
from .providers.base import Provider, ProviderError

logger = logging.getLogger("ttshub.router")

PROBE_TEXT = "Teste de áudio do TTS Hub."
PROBE_PRIORITY = -1000  # probe nunca passa na frente de pedido real
PROBE_CONSUMER = "probe"


class QueueTimeout(Exception):
    """Timeout estourou antes de conseguir vaga no provider."""


class UnknownProvider(Exception):
    pass


class ProviderDisabled(Exception):
    pass


class AllProvidersFailed(Exception):
    def __init__(self, attempts: list):
        super().__init__("Nenhum provider gerou o áudio")
        self.attempts = attempts


@dataclass
class SynthesisResult:
    audio: bytes
    media_type: str
    provider: str
    voice: str
    fallback: bool
    duration_ms: int
    attempts: list = field(default_factory=list)


class PrioritySlots:
    """Semáforo com fila de prioridade (maior primeiro; empate = ordem de chegada)."""

    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.active = 0
        self._heap: list = []
        self._seq = itertools.count()

    @property
    def waiting(self) -> int:
        return sum(1 for _, _, fut in self._heap if not fut.done())

    async def acquire(self, priority: int) -> None:
        if self.active < self.limit and self.waiting == 0:
            self.active += 1
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (-priority, next(self._seq), fut))
        try:
            await fut
        except asyncio.CancelledError:
            # A vaga pode ter sido entregue a nós no mesmo tick do cancelamento:
            # devolve, senão ela some.
            if fut.done() and not fut.cancelled():
                self.release()
            raise

    def release(self) -> None:
        while self._heap:
            _, _, fut = heapq.heappop(self._heap)
            if not fut.done():
                fut.set_result(True)  # vaga passa direto pro próximo (active igual)
                return
        self.active -= 1


@dataclass
class ProviderState:
    open_until: float = 0.0           # monotonic; breaker aberto até aqui
    down_since: Optional[float] = None  # monotonic; None = saudável
    down_alerted: bool = False
    consecutive_failures: int = 0
    last_ok_at: Optional[float] = None    # epoch, pro painel
    last_error_at: Optional[float] = None
    last_error: Optional[str] = None
    last_latency_ms: Optional[int] = None
    last_probe_at: Optional[float] = None
    last_probe_ok: Optional[bool] = None
    ok_count: int = 0
    error_count: int = 0
    fallback_count: int = 0

    def is_open(self, now: float) -> bool:
        return now < self.open_until


class Router:
    def __init__(self, store: ConfigStore, providers: dict):
        self.store = store
        self.providers: dict[str, Provider] = providers
        self.state = {name: ProviderState() for name in providers}
        self.slots = {
            name: PrioritySlots(p.max_concurrent)
            for name, p in providers.items()
            if p.max_concurrent
        }
        self._probing: set = set()
        for name in providers:
            metrics.provider_up.labels(name).set(1)

    # ------------------------------------------------------------------- cadeia

    def _chain(self) -> list:
        return [c for c in self.store.providers() if c["name"] in self.providers]

    def _label(self, name: str) -> str:
        return self.providers[name].label

    def _candidates(self, selection: str, allow_disabled: bool) -> tuple[list, Optional[str]]:
        chain = self._chain()
        enabled = [c for c in chain if c["enabled"]]
        primary = enabled[0]["name"] if enabled else None
        if selection == "auto":
            now = time.monotonic()
            healthy = [c for c in enabled if not self.state[c["name"]].is_open(now)]
            tripped = [c for c in enabled if self.state[c["name"]].is_open(now)]
            return healthy + tripped, primary
        if selection == "primary":
            return enabled[:1], primary
        cfg = next((c for c in chain if c["name"] == selection), None)
        if cfg is None:
            raise UnknownProvider(selection)
        if not cfg["enabled"] and not allow_disabled:
            raise ProviderDisabled(selection)
        return [cfg], primary

    # ---------------------------------------------------------------- tentativa

    async def _attempt(self, provider: Provider, cfg: dict, text: str, voice: str, speed: int, fmt: str, priority: int):
        acquired = False

        async def run():
            nonlocal acquired
            slots = self.slots.get(provider.name)
            if slots:
                metrics.queue_waiting.labels(provider.name).set(slots.waiting + 1)
                await slots.acquire(priority)
                metrics.queue_waiting.labels(provider.name).set(slots.waiting)
            acquired = True
            try:
                native = await provider.synthesize(text, voice, speed)
                return await audio.convert(native, provider.native_format, fmt)
            finally:
                if slots:
                    slots.release()

        try:
            return await asyncio.wait_for(run(), timeout=cfg["timeout_s"])
        except asyncio.TimeoutError:
            if not acquired:
                raise QueueTimeout(f"timeout de {cfg['timeout_s']:g}s esperando vaga (provider ocupado)")
            raise ProviderError(f"timeout de {cfg['timeout_s']:g}s")

    def _record_success(self, name: str, latency_ms: int) -> None:
        st = self.state[name]
        st.ok_count += 1
        st.consecutive_failures = 0
        st.open_until = 0.0
        st.last_ok_at = time.time()
        st.last_latency_ms = latency_ms
        if st.down_since is not None:
            if st.down_alerted:
                alerts.provider_up(name, self._label(name), time.monotonic() - st.down_since)
            logger.info("Provider %s voltou", name)
            st.down_since = None
            st.down_alerted = False
            metrics.provider_up.labels(name).set(1)

    def _record_failure(self, name: str, reason: str) -> None:
        st = self.state[name]
        now = time.monotonic()
        st.error_count += 1
        st.consecutive_failures += 1
        st.last_error = reason
        st.last_error_at = time.time()
        cfg = self.store.provider(name) or {}
        st.open_until = now + float(cfg.get("cooldown_s", 60))
        if st.down_since is None:
            st.down_since = now
            metrics.provider_up.labels(name).set(0)
            logger.warning("Provider %s caiu: %s", name, reason)
        # A cada falha enquanto a queda não foi avisada (envio pro Discord pode
        # ter falhado): o alerts deduplica e só marca down_alerted no sucesso.
        if cfg.get("enabled") and not st.down_alerted:
            nxt = next(
                (c["name"] for c in self._chain()
                 if c["enabled"] and c["name"] != name and self.state[c["name"]].down_since is None),
                None,
            )

            def _mark_alerted(st=st, episode=st.down_since):
                # Só vale pra mesma queda: se já voltou (ou caiu de novo), ignora.
                if st.down_since == episode:
                    st.down_alerted = True

            alerts.provider_down(name, self._label(name), reason, self._label(nxt) if nxt else None, _mark_alerted)

    # ------------------------------------------------------------------ síntese

    async def synthesize(
        self,
        *,
        text: str,
        fmt: str,
        consumer: str,
        priority: int = 0,
        voice: Optional[str] = None,
        speed: Optional[int] = None,
        selection: str = "auto",
        allow_disabled: bool = False,
        alert_on_failure: bool = True,
    ) -> SynthesisResult:
        candidates, primary = self._candidates(selection, allow_disabled)
        attempts = []
        started = time.monotonic()

        for cfg in candidates:
            name = cfg["name"]
            provider = self.providers[name]
            voice_id = provider.resolve_voice(voice, cfg["voice"])
            spd = speed if speed is not None else cfg["speed"]
            t0 = time.monotonic()
            try:
                data, media_type = await self._attempt(provider, cfg, text, voice_id, spd, fmt, priority)
            except QueueTimeout as err:
                metrics.requests_total.labels(consumer, name, "queue_timeout").inc()
                attempts.append({"provider": name, "error": str(err)})
                logger.warning("Fila do %s estourou (consumidor %s): %s", name, consumer, err)
                continue
            except Exception as err:  # ProviderError, ConversionError ou bug do provider
                reason = str(err) or err.__class__.__name__
                metrics.requests_total.labels(consumer, name, "error").inc()
                self._record_failure(name, reason)
                attempts.append({"provider": name, "error": reason})
                logger.warning("Provider %s falhou (consumidor %s): %s", name, consumer, reason)
                continue

            elapsed = time.monotonic() - t0
            latency_ms = int(elapsed * 1000)
            metrics.requests_total.labels(consumer, name, "ok").inc()
            metrics.synthesis_seconds.labels(name).observe(elapsed)
            self._record_success(name, latency_ms)
            fallback = selection == "auto" and name != primary
            if fallback:
                metrics.fallback_total.labels(consumer, name).inc()
                self.state[name].fallback_count += 1
            # Só tamanho, nunca o texto: tem nome e endereço de cliente.
            logger.info(
                "Áudio gerado consumer=%s provider=%s fallback=%s chars=%d bytes=%d ms=%d",
                consumer, name, fallback, len(text), len(data), latency_ms,
            )
            return SynthesisResult(
                audio=data,
                media_type=media_type,
                provider=name,
                voice=voice_id,
                fallback=fallback,
                duration_ms=int((time.monotonic() - started) * 1000),
                attempts=attempts,
            )

        metrics.failed_total.labels(consumer).inc()
        if alert_on_failure:
            alerts.all_failed(consumer, attempts)
        raise AllProvidersFailed(attempts)

    # ------------------------------------------------------------------- probes

    async def probe(self, name: str) -> dict:
        """Síntese de teste num provider (habilitado ou não). Atualiza o estado."""
        cfg = self.store.provider(name)
        provider = self.providers[name]
        st = self.state[name]
        t0 = time.monotonic()
        result = {"provider": name, "ok": False, "error": None, "latency_ms": None}
        try:
            voice_id = provider.resolve_voice(None, cfg["voice"])
            await self._attempt(provider, cfg, PROBE_TEXT, voice_id, cfg["speed"], "wav_8k", PROBE_PRIORITY)
        except QueueTimeout as err:
            # Ocupado com pedido real: não diz nada sobre a saúde.
            result["error"] = str(err)
        except Exception as err:
            reason = str(err) or err.__class__.__name__
            self._record_failure(name, f"probe: {reason}")
            st.last_probe_ok = False
            result["error"] = reason
        else:
            latency_ms = int((time.monotonic() - t0) * 1000)
            self._record_success(name, latency_ms)
            st.last_probe_ok = True
            result.update(ok=True, latency_ms=latency_ms)
        st.last_probe_at = time.time()
        return result

    async def probe_loop(self) -> None:
        """Testa cada provider habilitado: a cada PROBE_INTERVAL_S se saudável,
        a cada PROBE_INTERVAL_DOWN_S se caído. É o que detecta o fallback
        quebrado antes de alguém precisar dele."""
        while True:
            await asyncio.sleep(10)
            try:
                for cfg in self._chain():
                    name = cfg["name"]
                    if not cfg["enabled"] or name in self._probing:
                        continue
                    st = self.state[name]
                    interval = settings.PROBE_INTERVAL_DOWN_S if st.down_since is not None else settings.PROBE_INTERVAL_S
                    if st.last_probe_at is None or time.time() - st.last_probe_at >= interval:
                        self._probing.add(name)
                        task = asyncio.create_task(self.probe(name))
                        task.add_done_callback(lambda _t, n=name: self._probing.discard(n))
            except Exception:
                logger.exception("Erro no loop de probes")

    # ------------------------------------------------------------------- painel

    def status(self) -> list:
        now = time.monotonic()
        out = []
        for cfg in self._chain():
            name = cfg["name"]
            st = self.state[name]
            slots = self.slots.get(name)
            out.append({
                **self.providers[name].info(),
                "config": {k: cfg[k] for k in ("position", "enabled", "timeout_s", "cooldown_s", "voice", "speed")},
                "state": {
                    "healthy": st.down_since is None,
                    "breaker_open": st.is_open(now),
                    "breaker_open_for_s": max(0, round(st.open_until - now)) if st.is_open(now) else 0,
                    "down_for_s": round(now - st.down_since) if st.down_since is not None else 0,
                    "consecutive_failures": st.consecutive_failures,
                    "last_ok_at": st.last_ok_at,
                    "last_error_at": st.last_error_at,
                    "last_error": st.last_error,
                    "last_latency_ms": st.last_latency_ms,
                    "last_probe_at": st.last_probe_at,
                    "last_probe_ok": st.last_probe_ok,
                    "ok_count": st.ok_count,
                    "error_count": st.error_count,
                    "fallback_count": st.fallback_count,
                    "queue_active": slots.active if slots else None,
                    "queue_waiting": slots.waiting if slots else None,
                },
            })
        return out

    async def close(self) -> None:
        for p in self.providers.values():
            await p.close()
