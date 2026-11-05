"""TTS Hub — gateway de texto→áudio compartilhado entre os produtos.

API dos consumidores (API key):
  POST /v1/synthesize   texto → áudio (formato pedido), com fallback entre providers
  GET  /v1/providers    cadeia atual e saúde de cada provider

Painel ADM (sessão, login com a conta do Guardia):
  GET  /admin           tela
  /admin/api/...        status, ordem/liga-desliga, teste de voz, API keys, auditoria

Infra:
  GET  /health          o gateway está de pé (não testa providers)
  GET  /metrics         Prometheus (Bearer METRICS_TOKEN se configurado)
"""
import asyncio
import hmac
import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware

from . import audio, auth, settings
from .config_store import ConfigStore
from .providers import build_providers
from .router import AllProvidersFailed, ProviderDisabled, Router, UnknownProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ttshub")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    providers = build_providers()
    store = ConfigStore(settings.DB_PATH)
    store.sync_providers(providers)
    router = Router(store, providers)
    app.state.store = store
    app.state.router = router
    probe_task = asyncio.create_task(router.probe_loop()) if settings.PROBE_ENABLED else None
    logger.info("TTS Hub no ar. Cadeia: %s", [p["name"] for p in store.providers() if p["enabled"]])
    yield
    if probe_task:
        probe_task.cancel()
    await router.close()


session_secret = settings.SESSION_SECRET
if not session_secret:
    session_secret = secrets.token_hex(32)
    logger.warning("SESSION_SECRET vazio: usando um aleatório (sessões do painel caem a cada restart)")

app = FastAPI(title="TTS Hub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    session_cookie="ttshub_session",
    max_age=settings.SESSION_MAX_AGE_S,
    same_site="strict",
    https_only=settings.COOKIE_SECURE,
)
app.mount("/admin/static", StaticFiles(directory=STATIC_DIR), name="static")


def _audio_response(result) -> Response:
    return Response(
        content=result.audio,
        media_type=result.media_type,
        headers={
            "X-TTS-Provider": result.provider,
            "X-TTS-Voice": result.voice,
            "X-TTS-Fallback": "true" if result.fallback else "false",
            "X-TTS-Duration-Ms": str(result.duration_ms),
            "Cache-Control": "no-store",
        },
    )


def _failed_response(err: AllProvidersFailed) -> JSONResponse:
    return JSONResponse(status_code=503, content={"error": "Nenhum provider gerou o áudio", "attempts": err.attempts})


class SynthesizeIn(BaseModel):
    text: str = Field(min_length=1, max_length=settings.MAX_TEXT_CHARS)
    format: str
    voice: Optional[str] = Field(default=None, max_length=100)
    speed: Optional[int] = Field(default=None, ge=-50, le=50)
    provider: str = Field(default="auto", max_length=50)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("texto vazio")
        return v

    @field_validator("format")
    @classmethod
    def _known_format(cls, v: str) -> str:
        if v not in audio.FORMATS:
            raise ValueError(f"formato desconhecido; use um de {sorted(audio.FORMATS)}")
        return v


async def _synthesize(request: Request, body: SynthesizeIn, **kwargs) -> Response:
    router: Router = request.app.state.router
    try:
        result = await router.synthesize(
            text=body.text, fmt=body.format, voice=body.voice, speed=body.speed, selection=body.provider, **kwargs
        )
    except UnknownProvider:
        raise HTTPException(status_code=400, detail=f"Provider desconhecido: {body.provider}")
    except ProviderDisabled:
        raise HTTPException(status_code=409, detail=f"Provider desabilitado: {body.provider}")
    except AllProvidersFailed as err:
        return _failed_response(err)
    return _audio_response(result)


# ------------------------------------------------------------------ consumidores

@app.post("/v1/synthesize")
async def synthesize(body: SynthesizeIn, request: Request, consumer: dict = Depends(auth.require_consumer)):
    return await _synthesize(request, body, consumer=consumer["name"], priority=consumer["priority"])


@app.get("/v1/providers")
async def list_providers(request: Request, consumer: dict = Depends(auth.require_consumer)):
    status = request.app.state.router.status()
    return {
        "formats": sorted(audio.FORMATS),
        "providers": [
            {
                "name": p["name"],
                "label": p["label"],
                "enabled": p["config"]["enabled"],
                "position": p["config"]["position"],
                "healthy": p["state"]["healthy"],
                "voices": p["voices"],
            }
            for p in status
        ],
    }


# -------------------------------------------------------------------------- infra

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics_endpoint(request: Request):
    if settings.METRICS_TOKEN:
        header = request.headers.get("authorization", "")
        if not hmac.compare_digest(header.encode(), f"Bearer {settings.METRICS_TOKEN}".encode()):
            raise HTTPException(status_code=401, detail="unauthorized")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/admin")


# ------------------------------------------------------------------------- painel

@app.get("/admin", include_in_schema=False)
async def admin_page():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers={"Cache-Control": "no-store"})


class LoginIn(BaseModel):
    email: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


@app.post("/admin/api/login")
async def admin_login(body: LoginIn, request: Request):
    user = await auth.login(request, body.email.strip(), body.password)
    return {"user": {k: user[k] for k in ("email", "name", "role", "source")}}


@app.post("/admin/api/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/admin/api/me")
async def admin_me(user: dict = Depends(auth.require_admin)):
    return {"user": {k: user[k] for k in ("email", "name", "role", "source")}}


@app.get("/admin/api/status")
async def admin_status(request: Request, user: dict = Depends(auth.require_admin)):
    return {
        "instance": settings.INSTANCE,
        "formats": sorted(audio.FORMATS),
        "providers": request.app.state.router.status(),
    }


class OrderIn(BaseModel):
    names: list[str]


@app.put("/admin/api/providers/order")
async def admin_reorder(body: OrderIn, request: Request, user: dict = Depends(auth.require_admin)):
    try:
        request.app.state.store.reorder(body.names, auth.actor(user))
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {"ok": True}


class ProviderPatch(BaseModel):
    enabled: Optional[bool] = None
    timeout_s: Optional[float] = Field(default=None, ge=1, le=300)
    cooldown_s: Optional[int] = Field(default=None, ge=5, le=3600)
    voice: Optional[str] = Field(default=None, max_length=100)
    speed: Optional[int] = Field(default=None, ge=-50, le=50)


@app.patch("/admin/api/providers/{name}")
async def admin_update_provider(name: str, body: ProviderPatch, request: Request, user: dict = Depends(auth.require_admin)):
    store = request.app.state.store
    router: Router = request.app.state.router
    if name not in router.providers or not store.provider(name):
        raise HTTPException(status_code=404, detail="Provider não encontrado")
    fields = body.model_dump(exclude_unset=True)

    if fields.get("enabled") is False:
        others = [p for p in store.providers() if p["enabled"] and p["name"] != name]
        if not others:
            raise HTTPException(status_code=400, detail="Pelo menos um provider precisa ficar habilitado")

    if "voice" in fields:
        voice = (fields["voice"] or "").strip() or None
        provider = router.providers[name]
        if voice and provider.resolve_voice(voice, None) != provider.voices.get(voice, voice):
            raise HTTPException(status_code=400, detail=f"Voz desconhecida para {name}: {voice}")
        fields["voice"] = voice

    store.update_provider(name, fields, auth.actor(user))
    return {"ok": True}


@app.post("/admin/api/providers/{name}/probe")
async def admin_probe(name: str, request: Request, user: dict = Depends(auth.require_admin)):
    router: Router = request.app.state.router
    if name not in router.providers:
        raise HTTPException(status_code=404, detail="Provider não encontrado")
    return await router.probe(name)


@app.post("/admin/api/test")
async def admin_test(body: SynthesizeIn, request: Request, user: dict = Depends(auth.require_admin)):
    # Teste do painel: pode mirar provider desabilitado (testar antes de ligar)
    # e não alerta no Discord se falhar — quem testou está vendo o erro.
    return await _synthesize(
        request, body, consumer="painel", priority=0, allow_disabled=True, alert_on_failure=False
    )


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    priority: int = Field(default=0, ge=-100, le=100)


class KeyPatch(BaseModel):
    priority: int = Field(ge=-100, le=100)


@app.get("/admin/api/keys")
async def admin_keys(request: Request, user: dict = Depends(auth.require_admin)):
    return {"keys": request.app.state.store.keys()}


@app.post("/admin/api/keys")
async def admin_create_key(body: KeyIn, request: Request, user: dict = Depends(auth.require_admin)):
    return request.app.state.store.create_key(body.name.strip(), body.priority, auth.actor(user))


@app.patch("/admin/api/keys/{key_id}")
async def admin_update_key(key_id: int, body: KeyPatch, request: Request, user: dict = Depends(auth.require_admin)):
    request.app.state.store.update_key(key_id, body.priority, auth.actor(user))
    return {"ok": True}


@app.delete("/admin/api/keys/{key_id}")
async def admin_revoke_key(key_id: int, request: Request, user: dict = Depends(auth.require_admin)):
    request.app.state.store.revoke_key(key_id, auth.actor(user))
    return {"ok": True}


@app.get("/admin/api/audit")
async def admin_audit(request: Request, user: dict = Depends(auth.require_admin)):
    return {"entries": request.app.state.store.audit(50)}
