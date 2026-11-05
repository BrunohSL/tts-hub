"""Configuração por ambiente. Segredo vem de arquivo (Docker secret) via `<VAR>_FILE`.

Tudo que o painel muda em tempo real (ordem dos providers, liga/desliga, timeouts,
voz, velocidade, API keys) NÃO mora aqui: fica no SQLite (`config_store.py`).
Aqui só o que é da infraestrutura e muda com redeploy.
"""
import os


def _secret(name: str, default: str = "") -> str:
    """Lê `<name>_FILE` (caminho do Docker secret) e, na falta, `<name>`."""
    path = os.getenv(f"{name}_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return default
    return os.getenv(name, default)


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = os.getenv("DATA_DIR", "./data")
DB_PATH = os.path.join(DATA_DIR, "ttshub.db")

# Diretório de trabalho do ffmpeg. Em produção é tmpfs (RAM): o áudio tem nome e
# endereço de cliente e não pode ficar em disco.
WORK_DIR = os.getenv("WORK_DIR", "/tmp/ttshub")

# Nome da instância no rodapé dos alertas (ex.: "prod", "local").
INSTANCE = os.getenv("INSTANCE", "local")

# --- Providers -----------------------------------------------------------------
PIPER_URL = os.getenv("PIPER_URL", "http://ttshub_piper:8000")
# Tem que bater com o PIPER_MAX_CONCURRENT do container do Piper: a fila (com
# prioridade por consumidor) fica AQUI; o container só recebe o que vai rodar.
PIPER_MAX_CONCURRENT = int(os.getenv("PIPER_MAX_CONCURRENT", "1"))

# --- Probes (teste sintético de cada provider em background) -------------------
PROBE_ENABLED = _bool("PROBE_ENABLED", True)
PROBE_INTERVAL_S = int(os.getenv("PROBE_INTERVAL_S", "300"))       # provider OK
PROBE_INTERVAL_DOWN_S = int(os.getenv("PROBE_INTERVAL_DOWN_S", "30"))  # provider caído

# --- Alertas -------------------------------------------------------------------
DISCORD_WEBHOOK_URL = _secret("DISCORD_WEBHOOK_URL")
ALERT_COOLDOWN_S = int(os.getenv("ALERT_COOLDOWN_S", "900"))

# --- Painel ADM ----------------------------------------------------------------
SESSION_SECRET = _secret("SESSION_SECRET")
SESSION_MAX_AGE_S = int(os.getenv("SESSION_MAX_AGE_S", str(12 * 3600)))
COOKIE_SECURE = _bool("COOKIE_SECURE", True)

# Login com a conta do Guardia: o gateway pergunta ao Guardia se email+senha
# valem (endpoint de serviço, protegido por chave) e se o papel é ADM. Nenhum
# token do Guardia é emitido nem guardado aqui.
GUARDIA_AUTH_URL = os.getenv("GUARDIA_AUTH_URL", "").rstrip("/")
GUARDIA_SERVICE_KEY = _secret("GUARDIA_SERVICE_KEY")
ADMIN_ROLES = {r.strip() for r in os.getenv("ADMIN_ROLES", "ADM").split(",") if r.strip()}

# Acesso local de emergência (opcional): entra mesmo com o Guardia fora do ar.
# Sem senha configurada, não existe.
ADMIN_LOCAL_USER = os.getenv("ADMIN_LOCAL_USER", "admin")
ADMIN_LOCAL_PASSWORD = _secret("ADMIN_LOCAL_PASSWORD")

# --- Métricas ------------------------------------------------------------------
# Vazio = /metrics aberto (ok em dev). Em produção o domínio é público: configurar.
METRICS_TOKEN = _secret("METRICS_TOKEN")

# --- Limites da API ------------------------------------------------------------
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "3000"))
