from __future__ import annotations

import logging
import uuid

from contextlib import asynccontextmanager
from typing import Annotated

from pydantic import BaseModel
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.archive import TranscriptArchive
from app.backends import (
    AdapterBackend,
    AstrBotHttpBackend,
    BackendContext,
    OpenAIProxyBackend,
    StreamingChatSession,
)
from app.config import get_settings
from app.schemas import ChatCompletionRequest

logger = logging.getLogger("aran-adapter")
settings = get_settings()


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def _build_backend() -> AdapterBackend:
    if settings.backend_type == "openai_proxy":
        return OpenAIProxyBackend(settings)
    if settings.backend_type == "astrbot_http":
        return AstrBotHttpBackend(settings)
    raise RuntimeError(
        f"Unsupported ARAN_ADAPTER_BACKEND_TYPE: {settings.backend_type}"
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    _configure_logging()
    logger.info(
        "Starting %s v%s backend=%s host=%s port=%s",
        settings.app_name,
        settings.app_version,
        settings.backend_type,
        settings.host,
        settings.port,
    )
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

backend = _build_backend()
archive = TranscriptArchive(settings)


class BackupCreateRequest(BaseModel):
    label: str | None = None


async def require_bearer_token(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    if not settings.adapter_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ARAN_ADAPTER_TOKEN is not configured",
        )

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must use Bearer token",
        )

    if token.strip() != settings.adapter_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid adapter token",
        )
    return token.strip()


async def require_admin_backup_token(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    expected_token = settings.manual_backup_token or settings.adapter_token
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No backup token is configured",
        )

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must use Bearer token",
        )

    if token.strip() != expected_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid backup token",
        )
    return token.strip()


def _resolve_owner_id(payload: ChatCompletionRequest) -> str:
    if payload.owner_id and payload.owner_id.strip():
        return payload.owner_id.strip()
    if payload.user and payload.user.strip():
        return payload.user.strip()
    metadata = payload.metadata or {}
    for key in ("owner_id", "username", "user"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if settings.astrbot_username:
        return settings.astrbot_username
    return "anonymous"


def _resolve_session_id(request: Request, payload: ChatCompletionRequest) -> str:
    from_header = (request.headers.get("X-Session-Id") or "").strip()
    if from_header:
        return from_header

    if payload.session_id:
        return payload.session_id.strip()

    metadata = payload.metadata or {}
    meta_session_id = metadata.get("session_id")
    if isinstance(meta_session_id, str) and meta_session_id.strip():
        return meta_session_id.strip()

    return f"anon-{uuid.uuid4().hex}"


def _resolve_client_platform(request: Request, payload: ChatCompletionRequest) -> str:
    for header_name in ("X-Platform", "X-Client-Platform"):
        value = (request.headers.get(header_name) or "").strip()
        if value:
            return value.lower()

    metadata = payload.metadata or {}
    for key in ("platform", "client_platform", "source", "channel"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()

    return "unknown"


def _build_context(request: Request, payload: ChatCompletionRequest) -> BackendContext:
    return BackendContext(
        request_id=request.headers.get("X-Request-Id", f"req-{uuid.uuid4().hex}"),
        owner_id=_resolve_owner_id(payload),
        session_id=_resolve_session_id(request, payload),
        client_platform=_resolve_client_platform(request, payload),
        client_ip=request.client.host if request.client else "",
        raw_path=request.url.path,
    )


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
        "backend_type": settings.backend_type,
    }


@app.get("/readyz")
async def readyz() -> JSONResponse:
    checks = {
        "adapter_token_configured": bool(settings.adapter_token),
        "backend_type": settings.backend_type,
        "upstream_chat_url_configured": bool(settings.upstream_chat_url),
        "astrbot_target_url_configured": bool(settings.astrbot_target_url),
    }
    is_ready = checks["adapter_token_configured"] and (
        (settings.backend_type == "openai_proxy" and checks["upstream_chat_url_configured"])
        or (settings.backend_type == "astrbot_http" and checks["astrbot_target_url_configured"])
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ready" if is_ready else "not_ready", "checks": checks},
    )


async def _handle_chat_completion(
    payload: ChatCompletionRequest,
    request: Request,
) -> JSONResponse | StreamingResponse:
    context = _build_context(request, payload)

    if payload.stream and not backend.supports_stream:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"Streaming is not supported by backend type {settings.backend_type}",
        )

    logger.info(
        "Received chat request request_id=%s session_id=%s platform=%s path=%s model=%s",
        context.request_id,
        context.session_id,
        context.client_platform,
        context.raw_path,
        payload.model or "",
    )

    if payload.stream:
        try:
            stream_session = await backend.chat_complete_stream(payload, request.headers, context)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception(
                "Chat streaming request failed request_id=%s session_id=%s",
                context.request_id,
                context.session_id,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc
        return StreamingResponse(
            _stream_with_archive(stream_session, payload, context),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        result = await backend.chat_complete(payload, request.headers, context)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "Chat request failed request_id=%s session_id=%s",
            context.request_id,
            context.session_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    try:
        await archive.append_chat_completion(
            payload=payload,
            context=context,
            response_payload=result,
        )
    except Exception:
        logger.exception(
            "Transcript archive write failed request_id=%s session_id=%s",
            context.request_id,
            context.session_id,
        )

    return JSONResponse(content=result)


async def _stream_with_archive(
    stream_session: StreamingChatSession,
    payload: ChatCompletionRequest,
    context: BackendContext,
):
    try:
        async for chunk in stream_session.body_iterator:
            yield chunk
    finally:
        try:
            result = await stream_session.finalize()
        except Exception:
            logger.exception(
                "Stream finalize failed request_id=%s session_id=%s",
                context.request_id,
                context.session_id,
            )
            return

        try:
            await archive.append_chat_completion(
                payload=payload,
                context=context,
                response_payload=result,
            )
        except Exception:
            logger.exception(
                "Transcript archive write failed request_id=%s session_id=%s",
                context.request_id,
                context.session_id,
            )


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
@app.post("/api/v1/chat/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
    _: str = Depends(require_bearer_token),
) -> JSONResponse:
    return await _handle_chat_completion(payload, request)


@app.post("/admin/backups/create")
async def create_manual_backup(
    payload: BackupCreateRequest,
    _: str = Depends(require_admin_backup_token),
) -> JSONResponse:
    try:
        result = await archive.create_backup_bundle(label=payload.label)
    except Exception as exc:
        logger.exception("Manual backup creation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    return JSONResponse(content={"status": "ok", **result})
