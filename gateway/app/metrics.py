"""Métricas Prometheus (/metrics). Labels sem dado de cliente: só consumidor e provider."""
from prometheus_client import Counter, Gauge, Histogram

requests_total = Counter(
    "ttshub_requests_total",
    "Tentativas de síntese por consumidor, provider e desfecho (ok | error | queue_timeout)",
    ["consumer", "provider", "outcome"],
)

fallback_total = Counter(
    "ttshub_fallback_total",
    "Áudios entregues por um provider que não era o principal da cadeia",
    ["consumer", "provider"],
)

failed_total = Counter(
    "ttshub_failed_total",
    "Pedidos em que NENHUM provider gerou o áudio",
    ["consumer"],
)

synthesis_seconds = Histogram(
    "ttshub_synthesis_seconds",
    "Duração de uma síntese bem-sucedida (inclui espera na fila e conversão)",
    ["provider"],
    buckets=(0.5, 1, 2, 3, 5, 8, 13, 21, 34, 55, 90),
)

provider_up = Gauge(
    "ttshub_provider_up",
    "1 = provider saudável (circuit breaker fechado), 0 = caído",
    ["provider"],
)

queue_waiting = Gauge(
    "ttshub_queue_waiting",
    "Pedidos esperando vaga num provider com limite de concorrência",
    ["provider"],
)
