"""Autenticação: API key dos consumidores e sessão do painel ADM.

Consumidor (Guardia, produto novo...): `Authorization: Bearer tth_...` ou
`X-API-Key: tth_...`. A chave é criada no painel e mostrada uma vez só.

Painel: login com a conta do Guardia. O gateway pergunta ao Guardia (endpoint
de serviço `POST /api/internal/auth/verify`, protegido por X-Service-Key) se o
email+senha valem e qual o papel; só ADMIN_ROLES entram. Nenhum token do
Guardia é emitido nem guardado: o painel tem sessão própria (cookie assinado).
Quando existir login unificado (IdP), troca só `_verify_with_guardia`.

Acesso local de emergência: ADMIN_LOCAL_USER + ADMIN_LOCAL_PASSWORD, pra entrar
com o Guardia fora do ar. Sem senha configurada, não existe.
"""
import hmac
import logging
import time
from collections import defaultdict, deque
from typing import Optional

import httpx
from fastapi import HTTPException, Request

from . import settings

logger = logging.getLogger("ttshub.auth")

LOGIN_WINDOW_S = 15 * 60
LOGIN_MAX_ATTEMPTS = 10
TOUCH_INTERVAL_S = 60

_login_attempts: dict = defaultdict(deque)
_last_touch: dict = {}


# ------------------------------------------------------------------ consumidores

def _extract_key(request: Request) -> Optional[str]:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-api-key")


def require_consumer(request: Request) -> dict:
    raw = _extract_key(request)
    store = request.app.state.store
    consumer = store.consumer_for(raw) if raw else None
    if not consumer:
        raise HTTPException(status_code=401, detail="API key ausente ou inválida")
    now = time.monotonic()
    if now - _last_touch.get(consumer["id"], -1e9) >= TOUCH_INTERVAL_S:
        _last_touch[consumer["id"]] = now
        store.touch_key(consumer["id"])
    return consumer


# ------------------------------------------------------------------------ painel

def require_admin(request: Request) -> dict:
    user = request.session.get("user")
    if not user or user.get("exp", 0) < time.time():
        request.session.clear()
        raise HTTPException(status_code=401, detail="Sessão expirada. Entre de novo.")
    # Defesa extra contra CSRF além do SameSite=Strict: escrita só da mesma origem.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
            raise HTTPException(status_code=403, detail="Origem não permitida")
    return user


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    attempts = _login_attempts[ip]
    while attempts and now - attempts[0] > LOGIN_WINDOW_S:
        attempts.popleft()
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        return True
    attempts.append(now)
    return False


async def _verify_with_guardia(email: str, password: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(
                f"{settings.GUARDIA_AUTH_URL}/api/internal/auth/verify",
                json={"email": email, "password": password},
                headers={"X-Service-Key": settings.GUARDIA_SERVICE_KEY},
            )
    except httpx.HTTPError as err:
        logger.warning("Guardia inacessível no login do painel: %s", err)
        raise HTTPException(status_code=502, detail="Guardia indisponível para validar o login. Use o acesso local de emergência.")

    try:
        body = res.json()
    except ValueError:
        body = {}
    if res.status_code != 200:
        if res.status_code in (401, 403, 423):
            raise HTTPException(status_code=401, detail=body.get("error") or "Email ou senha inválidos")
        logger.warning("Guardia respondeu %s no login do painel", res.status_code)
        raise HTTPException(status_code=502, detail="Erro ao validar o login no Guardia")

    user = (body.get("data") or {}).get("user") or {}
    if user.get("role") not in settings.ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Sua conta não tem acesso ao painel do TTS Hub")
    return {"email": user.get("email") or email, "name": user.get("name") or email, "role": user["role"], "source": "guardia"}


async def login(request: Request, email: str, password: str) -> dict:
    ip = request.client.host if request.client else "?"
    if _rate_limited(ip):
        raise HTTPException(status_code=429, detail="Muitas tentativas. Tente de novo em alguns minutos.")

    if settings.ADMIN_LOCAL_PASSWORD and email == settings.ADMIN_LOCAL_USER:
        if hmac.compare_digest(password.encode(), settings.ADMIN_LOCAL_PASSWORD.encode()):
            user = {"email": email, "name": "Acesso local", "role": "ADM", "source": "local"}
            logger.warning("Login no painel com o acesso LOCAL de emergência (ip=%s)", ip)
        else:
            raise HTTPException(status_code=401, detail="Email ou senha inválidos")
    elif settings.GUARDIA_AUTH_URL and settings.GUARDIA_SERVICE_KEY:
        user = await _verify_with_guardia(email, password)
    else:
        raise HTTPException(status_code=503, detail="Login do painel não configurado (GUARDIA_AUTH_URL / ADMIN_LOCAL_PASSWORD)")

    user["exp"] = time.time() + settings.SESSION_MAX_AGE_S
    request.session.clear()
    request.session["user"] = user
    logger.info("Login no painel: %s (%s)", user["email"], user["source"])
    return user


def actor(user: dict) -> str:
    return f"{user['email']} ({user['source']})"
