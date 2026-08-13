from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from claude_code_backend.model_config import public_model_options
from claude_code_backend.models import (
    ChatRequest,
    ChatResponse,
    ContextResponse,
    ContextUpdateRequest,
    HealthResponse,
    SessionCreateRequest,
    SessionState,
)
from claude_code_backend.service import LocalAgentService


def create_local_agent_router(service: LocalAgentService | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/local-agent", tags=["local-agent"])
    service = service or LocalAgentService()
    service_settings = service.config

    @router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            enabled=service_settings.enabled,
            provider=service_settings.provider_name,
            default_model=service_settings.default_model_key,
            base_url=service_settings.base_url,
            model_options=public_model_options(service_settings.runtime_model),
            routes=[
                "GET /api/local-agent/health",
                "GET /api/local-agent/models",
                "POST /api/local-agent/sessions",
                "GET /api/local-agent/sessions/{session_id}",
                "GET /api/local-agent/sessions/{session_id}/context",
                "PUT /api/local-agent/sessions/{session_id}/context",
                "POST /api/local-agent/chat",
                "POST /api/local-agent/chat/stream",
            ],
        )

    @router.get("/models")
    async def models() -> dict:
        return {
            "items": public_model_options(service_settings.runtime_model),
            "default_model": service_settings.default_model_key,
            "endpoint_configured": bool(service_settings.base_url),
            "local_only": False,
        }

    @router.post("/sessions", response_model=SessionState)
    async def create_session(payload: SessionCreateRequest) -> SessionState:
        return service.create_session(payload)

    @router.get("/sessions/{session_id}", response_model=SessionState)
    async def get_session(session_id: str) -> SessionState:
        session = service.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return session

    @router.get("/sessions/{session_id}/context", response_model=ContextResponse)
    async def get_context(session_id: str) -> ContextResponse:
        context = service.get_context(session_id)
        if context is None:
            raise HTTPException(status_code=404, detail="session not found")
        return context

    @router.put("/sessions/{session_id}/context", response_model=ContextResponse)
    async def update_context(session_id: str, payload: ContextUpdateRequest) -> ContextResponse:
        context = service.update_context(session_id, payload)
        if context is None:
            raise HTTPException(status_code=404, detail="session not found")
        return context

    @router.post("/chat", response_model=ChatResponse)
    async def chat(payload: ChatRequest) -> ChatResponse:
        return await service.chat(payload)

    @router.post("/chat/stream")
    async def chat_stream(payload: ChatRequest) -> StreamingResponse:
        response = await service.chat(payload)

        async def event_stream():
            data = response.model_dump(mode="json")
            yield f"event: message\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return router
