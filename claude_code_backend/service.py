from __future__ import annotations

from claude_code_backend.config import LocalAgentSettings, settings
from claude_code_backend.models import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ContextEvent,
    ContextResponse,
    ContextUpdateRequest,
    SessionCreateRequest,
    SessionState,
    utc_now,
)
from claude_code_backend.provider import LocalModelClient, LocalModelError, build_default_client
from claude_code_backend.session_store import JsonFileSessionStore, SessionStore
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from personal_news_agent.config import Settings


class LocalAgentService:
    def __init__(
        self,
        store: SessionStore | None = None,
        client: LocalModelClient | None = None,
        config: LocalAgentSettings = settings,
    ) -> None:
        self.config = config
        self.store = store or JsonFileSessionStore(config.session_store_path)
        self.client = client or build_default_client(config)

    @classmethod
    def from_app_settings(cls, app_settings: Settings) -> "LocalAgentService":
        """Build the compatibility agent from the application's one LLM contract."""
        config = LocalAgentSettings(
            provider_name="deepseek",
            base_url=app_settings.llm_endpoint or "https://api.deepseek.com",
            default_model_key=app_settings.llm_default_model,
            runtime_model=app_settings.llm_model,
            api_key=app_settings.llm_key,
            timeout_seconds=app_settings.llm_timeout_seconds,
        )
        return cls(config=config)

    def create_session(self, payload: SessionCreateRequest) -> SessionState:
        session = SessionState(
            user_id=payload.user_id,
            title=payload.title,
            project_context=payload.project_context,
            metadata=payload.metadata,
        )
        if payload.project_context:
            session.context_events.append(
                ContextEvent(source="session_create", values=payload.project_context, summary="initial context")
            )
        self.store.save(session)
        return session

    def get_session(self, session_id: str) -> SessionState | None:
        return self.store.get(session_id)

    def get_context(self, session_id: str) -> ContextResponse | None:
        session = self.store.get(session_id)
        if session is None:
            return None
        return ContextResponse(
            session_id=session.session_id,
            project_context=session.project_context,
            context_events=session.context_events,
        )

    def update_context(self, session_id: str, payload: ContextUpdateRequest) -> ContextResponse | None:
        session = self.store.get(session_id)
        if session is None:
            return None
        if payload.replace:
            session.project_context = dict(payload.values)
        else:
            session.project_context.update(payload.values)
        session.context_events.append(
            ContextEvent(
                source=payload.source,
                summary=payload.summary,
                values=payload.values,
                metadata=payload.metadata,
            )
        )
        session.updated_at = utc_now()
        self.store.save(session)
        return ContextResponse(
            session_id=session.session_id,
            project_context=session.project_context,
            context_events=session.context_events,
        )

    async def chat(self, payload: ChatRequest) -> ChatResponse:
        session = self._resolve_session(payload)
        user_message = ChatMessage(
            role="user",
            content=payload.message,
            metadata={
                "attachments": [attachment.model_dump(mode="json") for attachment in payload.attachments],
                "request_metadata": payload.metadata,
            },
        )
        session.messages.append(user_message)
        if payload.project_context:
            session.project_context.update(payload.project_context)
            session.context_events.append(
                ContextEvent(
                    source="chat_request",
                    summary="context supplied with chat request",
                    values=payload.project_context,
                    metadata={"model_key": payload.model_key, "user_id": payload.user_id},
                )
            )
        session.updated_at = utc_now()

        history = session.messages[-self.config.max_history_messages :]
        provider_request = payload.model_copy(update={"session_id": session.session_id})
        try:
            text, provider_metadata = await self.client.complete(provider_request, history)
            status = "ok"
        except LocalModelError as exc:
            text = str(exc)
            provider_metadata = {
                "provider": self.config.provider_name,
                "model_key": payload.model_key or self.config.default_model_key,
                "base_url": self.config.base_url,
            }
            status = "error"

        assistant_message = ChatMessage(role="assistant", content=text, metadata={"provider": provider_metadata})
        session.messages.append(assistant_message)
        session.updated_at = utc_now()
        self.store.save(session)

        return ChatResponse(
            session_id=session.session_id,
            message=assistant_message,
            status=status,
            provider_metadata=provider_metadata,
        )

    def _resolve_session(self, payload: ChatRequest) -> SessionState:
        if payload.session_id:
            session = self.store.get(payload.session_id)
            if session:
                return session
        session = SessionState(user_id=payload.user_id, project_context=payload.project_context)
        self.store.save(session)
        return session
