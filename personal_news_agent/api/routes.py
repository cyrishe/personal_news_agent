from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from personal_news_agent.api.schemas import (
    ChatRequest,
    DeepDiveRequest,
    DueCrawlRequest,
    DueTasksRequest,
    FeedbackRequest,
    LoginRequest,
    NativeSearchIngestRequest,
    NotificationReadRequest,
    OnboardingRequest,
    ProfileRequest,
    RegistrationCodeRequest,
    RelatedSearchRequest,
    RegisterRequest,
    ReportRequest,
    ScheduleCommandRequest,
    TaskEnabledRequest,
    SearchRequest,
    TaskRequest,
    TurnRelationRequest,
    TopicCreateRequest,
    TopicSummaryRequest,
    TopicViewRequest,
    mask_mobile,
    parse_range,
)
from personal_news_agent.config import Settings
from personal_news_agent.core.categories import CATEGORIES
from personal_news_agent.services.auth import AuthError
from personal_news_agent.services.report_export import export_report
from personal_news_agent.services.model_config import DEFAULT_LOGICAL_MODEL


FRONTEND_REVISION = "20260812-chat-workspace-1"
NO_CACHE_PAGE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "X-PNA-Frontend-Revision": FRONTEND_REVISION,
}


def register_routes(app: FastAPI, services: dict[str, Any], static_dir: Path, settings: Settings) -> None:
    registry = services["registry"]
    store = services["store"]
    url_store = services["url_store"]
    search_index = services["search_index"]
    search_service = services["search"]
    events = services["events"]

    async def run_phone_operation(operation: str, callback: Any) -> Any:
        timeout_seconds = min(60.0, max(0.1, float(settings.phone_challenge_request_timeout_seconds)))
        started_at = time.monotonic()
        provider = services["auth"].phone_registration_status().get("provider") or "unknown"
        _log_phone_operation(store, operation, "started", provider, 0)
        try:
            result = await asyncio.wait_for(asyncio.to_thread(callback), timeout=timeout_seconds)
        except TimeoutError as exc:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            _log_phone_operation(store, operation, "timeout", provider, elapsed_ms)
            raise HTTPException(
                status_code=504,
                detail={"code": "phone_code_send_timeout", "message": "短信服务响应超时，请稍后重试。"},
            ) from exc
        except AuthError:
            raise
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            _log_phone_operation(store, operation, "error", provider, elapsed_ms, code=type(exc).__name__)
            raise
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        _log_phone_operation(store, operation, "ok", provider, elapsed_ms)
        return result

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "landing.html", headers=NO_CACHE_PAGE_HEADERS)

    @app.get("/web")
    async def web_app() -> FileResponse:
        return FileResponse(static_dir / "home.html", headers=NO_CACHE_PAGE_HEADERS)

    @app.get("/auth")
    async def auth_app() -> FileResponse:
        return FileResponse(static_dir / "auth.html", headers=NO_CACHE_PAGE_HEADERS)

    @app.get("/mobile")
    async def mobile_app() -> FileResponse:
        return FileResponse(static_dir / "home.html", headers=NO_CACHE_PAGE_HEADERS)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "frontend_revision": FRONTEND_REVISION,
            "categories": CATEGORIES,
            "source_count": len(registry.all_sources()),
        }

    @app.get("/api/models")
    async def models() -> dict[str, Any]:
        return {
            "items": services["model_options"](),
            "default_model": DEFAULT_LOGICAL_MODEL,
            "endpoint_configured": bool(settings.llm_endpoint),
        }

    @app.get("/api/sources")
    async def sources(category: str | None = None) -> dict[str, Any]:
        try:
            selected = registry.get_sources_by_category(category) if category else registry.all_sources()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "items": [
                {
                    "source_id": source.source_id,
                    "name": source.name,
                    "categories": source.categories,
                    "tags": source.tags,
                    "region": source.region,
                    "language": source.language,
                    "credibility": source.credibility,
                    "crawl_interval_minutes": source.crawl_interval_minutes,
                    "crawl_enabled": source.crawl_enabled,
                    "search_enabled": source.search_enabled,
                    "sections": [section.__dict__ for section in source.sections],
                }
                for source in selected
            ]
        }

    @app.get("/api/sources/summary")
    async def source_summary() -> dict[str, Any]:
        return registry.source_summary() | {"inventory": store.list_source_inventory()}

    @app.get("/api/crawl/due")
    async def crawl_due_plan(category: str | None = None, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        return services["crawl"].due_plan(category=category, limit=limit)

    @app.get("/api/crawl/urls/due")
    async def crawl_due_urls(category: str | None = None, limit: int = Query(default=50, ge=1, le=200), url_type: str | None = "article") -> dict[str, Any]:
        return {"items": url_store.list_due(category=category, limit=limit, url_type=url_type), "mysql_ready": url_store.ready}

    @app.post("/api/crawl/due")
    async def crawl_due(payload: DueCrawlRequest) -> dict[str, Any]:
        return await services["crawl"].crawl_due(
            category=payload.category,
            limit=payload.limit,
            per_section_limit=payload.per_section_limit,
            fetch_articles=payload.fetch_articles,
            workers=payload.workers,
        )

    @app.get("/api/feed")
    async def feed(category: str | None = None, limit: int = Query(default=20, ge=1, le=100), user_id: str = "default") -> dict[str, Any]:
        return {"items": services["feed"].feed(user_id=user_id, category=category, limit=limit)}

    @app.post("/api/profile")
    async def save_profile(payload: ProfileRequest) -> dict[str, Any]:
        store.save_profile(payload.model_dump())
        return {"status": "ok", "profile": store.get_profile(payload.user_id)}

    @app.get("/api/profile")
    async def get_profile(user_id: str = "default") -> dict[str, Any]:
        user = store.get_user(user_id)
        profile = store.get_profile(user_id)
        return {
            "user": {
                "id": user["id"],
                "username": user.get("username"),
                "display_name": user.get("display_name"),
                "mobile": mask_mobile(user.get("mobile") or ""),
                "realname_verified": bool(user.get("realname_verified")),
                "realname_provider": user.get("realname_provider"),
                "assistant_prompt": user.get("assistant_prompt"),
            }
            if user
            else None,
            "profile": profile,
        }

    @app.post("/api/auth/register")
    async def register(payload: RegisterRequest) -> dict[str, Any]:
        try:
            return await run_phone_operation(
                "phone_registration",
                lambda: services["auth"].register_phone(
                    mobile=payload.mobile,
                    challenge_id=payload.challenge_id,
                    verification_code=payload.verification_code,
                    password=payload.password,
                    confirm_password=payload.confirm_password,
                ),
            )
        except AuthError as exc:
            _log_phone_operation(
                store,
                "phone_registration",
                "rejected",
                services["auth"].phone_registration_status().get("provider") or "unknown",
                0,
                code=exc.code,
            )
            headers = None
            if getattr(exc, "retry_after_seconds", None):
                headers = {"Retry-After": str(max(1, int(exc.retry_after_seconds)))}
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
                headers=headers,
            ) from exc

    @app.get("/api/auth/config")
    async def auth_config() -> dict[str, Any]:
        return services["auth"].phone_registration_status()

    @app.post("/api/auth/registration-code")
    async def registration_code(payload: RegistrationCodeRequest, request: Request) -> dict[str, Any]:
        try:
            return await run_phone_operation(
                "phone_registration_code",
                lambda: services["auth"].request_registration_code(
                    payload.mobile,
                    remote_addr=_request_remote_addr(request),
                ),
            )
        except AuthError as exc:
            _log_phone_operation(
                store,
                "phone_registration_code",
                "rejected",
                services["auth"].phone_registration_status().get("provider") or "unknown",
                0,
                code=exc.code,
            )
            headers = None
            if getattr(exc, "retry_after_seconds", None):
                headers = {"Retry-After": str(max(1, int(exc.retry_after_seconds)))}
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
                headers=headers,
            ) from exc

    @app.post("/api/auth/login")
    async def login(payload: LoginRequest) -> dict[str, Any]:
        try:
            return services["auth"].login(payload.mobile or payload.username or "", payload.password)
        except AuthError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.get("/api/auth/realname/status")
    async def realname_status() -> dict[str, Any]:
        return services["auth"].realname.status()

    @app.get("/api/onboarding/options")
    async def onboarding_options() -> dict[str, Any]:
        return services["onboarding"].options()

    @app.post("/api/onboarding/complete")
    async def onboarding_complete(payload: OnboardingRequest) -> dict[str, Any]:
        try:
            return services["onboarding"].complete(payload.user_id, payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/auth/wechat/status")
    async def wechat_status() -> dict[str, Any]:
        return services["auth"].wechat_status()

    @app.get("/api/auth/wechat/login-url")
    async def wechat_login_url(mode: str | None = None, state_param: str | None = Query(default=None, alias="state"), redirect_uri: str | None = None) -> dict[str, Any]:
        try:
            return services["auth"].wechat_login_url(mode=mode, state=state_param, redirect_uri=redirect_uri)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/auth/wechat/callback")
    async def wechat_callback(code: str, state_param: str | None = Query(default=None, alias="state")) -> dict[str, Any]:
        try:
            return await services["auth"].wechat_callback(code=code, state=state_param)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/feedback")
    async def feedback(payload: FeedbackRequest) -> dict[str, Any]:
        store.save_feedback(payload.user_id, payload.target_type, payload.target_id, payload.feedback_type)
        return {"status": "ok"}

    @app.post("/api/chat/turns/{turn_id}/relation")
    async def set_turn_relation(turn_id: str, payload: TurnRelationRequest) -> dict[str, Any]:
        if payload.relation not in {"related", "unrelated"}:
            raise HTTPException(status_code=400, detail="relation must be related or unrelated")
        item = store.set_turn_relation(turn_id, payload.user_id, payload.relation, services["chat"].topic_drift_notice)
        if not item:
            raise HTTPException(status_code=404, detail="turn not found")
        return {"item": item, "response": item.get("response")}

    @app.post("/api/news/search")
    async def search(payload: SearchRequest) -> dict[str, Any]:
        time_range = parse_range(payload.time_range)
        results = await search_service.search(
            payload.query,
            payload.category_scope,
            payload.source_scope,
            time_range,
            payload.max_results,
            include_remote=payload.allow_web_search,
        )
        return {"items": results}

    @app.post("/api/news/deep-dive")
    async def deep_dive(payload: DeepDiveRequest) -> dict[str, Any]:
        return await services["deep_dive"].run(
            payload.query,
            category_scope=payload.category_scope,
            source_scope=payload.source_scope,
            rounds=payload.rounds,
            breadth=payload.breadth,
            include_remote=payload.allow_web_search,
        )

    @app.post("/api/news/related")
    async def related_search(payload: RelatedSearchRequest) -> Any:
        return await services["chat"].related_search(
            payload.conversation_id,
            payload.query,
            topic=payload.topic,
            category_scope=payload.category_scope,
            user_id=payload.user_id,
            max_queries=payload.max_queries,
            allow_web_search=payload.allow_web_search,
        )

    @app.post("/api/news/search/ingest")
    async def native_search_ingest(payload: NativeSearchIngestRequest) -> dict[str, Any]:
        return await services["native_ingestion"].ingest(
            query=payload.query,
            category_scope=payload.category_scope,
            source_scope=payload.source_scope,
            max_results=payload.max_results,
            fetch_articles=payload.fetch_articles,
            follow_depth=payload.follow_depth,
            follow_limit_per_article=payload.follow_limit_per_article,
        )

    @app.post("/api/topics/view")
    async def topic_view(payload: TopicViewRequest) -> dict[str, Any]:
        return await services["topic_views"].build(
            topic=payload.topic,
            category_scope=payload.category_scope,
            source_scope=payload.source_scope,
            max_articles=payload.max_articles,
        )

    @app.post("/api/topics/summary")
    async def topic_summary(payload: TopicSummaryRequest) -> dict[str, Any]:
        return await services["topic_summary"].generate(
            user_id=payload.user_id,
            topic=payload.topic,
            category_scope=payload.category_scope,
            source_scope=payload.source_scope,
            max_articles=payload.max_articles,
            use_llm=payload.use_llm,
            output_style=payload.output_style,
            save_report=payload.save_report,
        )

    @app.get("/api/topics")
    async def list_topics(
        user_id: str = "default",
        topic_type: str | None = None,
        conversation_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        return {
            "items": services["topic_agent"].list_topics(
                user_id=user_id,
                topic_type=topic_type,
                limit=limit,
                conversation_id=conversation_id,
            )
        }

    @app.get("/api/topics/recommended")
    async def recommended_topics(
        user_id: str = "default",
        limit: int = Query(default=6, ge=1, le=12),
        window_hours: int = Query(default=24, ge=3, le=72),
        refresh_window_hours: int = Query(default=6, ge=1, le=24),
        use_llm: bool = True,
        prefer_cached: bool = False,
    ) -> dict[str, Any]:
        if refresh_window_hours > window_hours:
            raise HTTPException(status_code=400, detail="refresh_window_hours cannot exceed window_hours")
        return await services["trending_topics"].recommend(
            user_id,
            limit=limit,
            window_hours=window_hours,
            refresh_window_hours=refresh_window_hours,
            use_llm=use_llm,
            prefer_cached=prefer_cached,
        )

    @app.post("/api/topics")
    async def create_topic(payload: TopicCreateRequest) -> dict[str, Any]:
        try:
            if payload.text:
                return await services["topic_agent"].create_topic_from_text(
                    user_id=payload.user_id,
                    text=payload.text,
                    category_scope=payload.category_scope,
                    schedule=payload.schedule,
                    refresh_now=payload.refresh_now,
                    conversation_id=payload.conversation_id,
                )
            return await services["topic_agent"].create_topic(
                user_id=payload.user_id,
                title=payload.title or "",
                category_scope=payload.category_scope,
                schedule=payload.schedule,
                topic_type=payload.topic_type,
                refresh_now=payload.refresh_now,
                conversation_id=payload.conversation_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/news/search/backend")
    async def search_backend() -> dict[str, Any]:
        return {
            "configured_backend": settings.search_backend,
            "local_backend": "sqlite_fts",
            "primary_recall_backend": "elasticsearch" if search_index.configured else "sqlite_fts",
            "external_provider": settings.external_search_provider,
            "external_configured": search_service.external_configured,
            "external_search_available": bool(
                search_service.external_configured
                or (
                    getattr(services.get("cc_runtime"), "configured", False)
                    and settings.cc_runtime_builtin_web_search
                )
            ),
            "elasticsearch": await search_index.health(),
            "crawl_url_store": {
                "backend": settings.crawl_url_backend,
                "mysql_configured": bool(settings.crawl_database_url),
                "mysql_ready": url_store.ready,
            },
        }

    def _event_payload(item: Any) -> dict[str, Any]:
        data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        article_ids = data.get("article_ids") or []
        article = store.get_article(article_ids[0]) if article_ids else None
        if article:
            data["source_url"] = article.get("url")
            data["source_title"] = article.get("title")
            data["source_id"] = article.get("source_id")
            data["source_published_at"] = article.get("published_at")
        return data

    @app.get("/api/events")
    async def list_events(category: str | None = None, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        clusters = events.discover(category=category, limit=limit)
        return {"items": [_event_payload(item) for item in clusters]}

    @app.post("/api/chat")
    async def chat(payload: ChatRequest) -> Any:
        return await services["chat"].chat(
            payload.conversation_id,
            payload.message,
            payload.topic,
            payload.category_scope,
            payload.use_llm,
            user_id=payload.user_id,
            allow_web_search=payload.allow_web_search,
            model_key=payload.model_key,
        )

    @app.get("/api/chat/conversations")
    async def chat_conversations(
        user_id: str = "default",
        limit: int = Query(default=30, ge=1, le=100),
    ) -> dict[str, Any]:
        return {"items": store.list_conversations(user_id=user_id, limit=limit)}

    @app.get("/api/chat/conversations/{conversation_id}")
    async def chat_history(
        conversation_id: str,
        user_id: str = "default",
        limit: int = Query(default=40, ge=1, le=100),
    ) -> dict[str, Any]:
        turns = store.list_turns(conversation_id, user_id=user_id, limit=limit)
        last = turns[-1] if turns else None
        return {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "turns": turns,
            "context": {
                "topic": (last or {}).get("topic"),
                "category_scope": (last or {}).get("category_scope") or [],
            },
        }

    @app.post("/api/chat/stream")
    async def chat_stream(payload: ChatRequest) -> StreamingResponse:
        async def event_stream():
            async for event in services["chat"].chat_events(
                payload.conversation_id,
                payload.message,
                payload.topic,
                payload.category_scope,
                payload.use_llm,
                user_id=payload.user_id,
                allow_web_search=payload.allow_web_search,
                model_key=payload.model_key,
            ):
                event_type = event.get("type", "message")
                data = json.dumps(event, ensure_ascii=False, default=str)
                yield f"event: {event_type}\ndata: {data}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/reports")
    async def reports(payload: ReportRequest) -> Any:
        return await services["reports"].generate(payload.user_id, payload.topic, payload.category_scope, payload.time_range, payload.report_type)

    @app.get("/api/reports/{report_id}/download")
    async def download_report(report_id: str, format: str = Query(default="pdf", pattern="^(pdf|docx)$"), user_id: str = "default") -> Response:
        report = store.get_report(report_id, user_id=user_id) or store.get_report(report_id)
        if not report:
            raise HTTPException(status_code=404, detail="report not found")
        try:
            exported = export_report(_hydrate_report_for_export(report, store), format)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content=exported.content,
            media_type=exported.media_type,
            headers={"Content-Disposition": f'attachment; filename="{exported.filename}"'},
        )

    @app.post("/api/tasks")
    async def create_task(payload: TaskRequest) -> dict[str, Any]:
        try:
            return services["tasks"].create_task(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/tasks/schedule")
    async def create_schedule_task(payload: ScheduleCommandRequest) -> dict[str, Any]:
        try:
            return await services["tasks"].create_from_schedule_message(payload.user_id, payload.message)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/tasks")
    async def list_tasks(user_id: str = "default", limit: int = Query(default=50, ge=1, le=100)) -> dict[str, Any]:
        return {"items": services["tasks"].list_tasks(user_id=user_id, limit=limit)}

    @app.post("/api/tasks/{task_id}/enabled")
    async def set_task_enabled(task_id: str, payload: TaskEnabledRequest) -> dict[str, Any]:
        item = services["tasks"].set_task_enabled(task_id, payload.user_id, payload.enabled)
        if not item:
            raise HTTPException(status_code=404, detail="task not found")
        return {"item": item}

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str, user_id: str = "default") -> dict[str, Any]:
        item = services["tasks"].delete_task(task_id, user_id)
        if not item:
            raise HTTPException(status_code=404, detail="task not found")
        return {"item": item}

    @app.get("/api/conversations")
    async def list_conversations(user_id: str = "default", limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        return {"items": store.list_conversations(user_id=user_id, limit=limit)}

    @app.get("/api/conversations/{conversation_id}/turns")
    async def list_conversation_turns(conversation_id: str, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        return {"items": store.list_turns(conversation_id=conversation_id, limit=limit)}

    @app.post("/api/tasks/due/run")
    async def run_due_tasks(payload: DueTasksRequest) -> dict[str, Any]:
        return await services["tasks"].run_due_tasks(user_id=payload.user_id, limit=payload.limit)

    @app.post("/api/tasks/{task_id}/run")
    async def run_task(task_id: str) -> dict[str, Any]:
        try:
            return await services["tasks"].run_task(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/notifications")
    async def notifications(user_id: str = "default", unread_only: bool = False, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        return {"items": store.list_notifications(user_id=user_id, unread_only=unread_only, limit=limit)}

    @app.post("/api/notifications/{notification_id}/read")
    async def read_notification(notification_id: str, payload: NotificationReadRequest) -> dict[str, Any]:
        item = store.mark_notification_read(notification_id, payload.user_id)
        if not item:
            raise HTTPException(status_code=404, detail="notification not found")
        return {"item": item}

    @app.post("/api/crawl/{category}")
    async def crawl(category: str) -> dict[str, Any]:
        try:
            return await services["crawl"].crawl_category(category)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


def _request_remote_addr(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if peer in {"127.0.0.1", "::1"}:
        forwarded = str(request.headers.get("x-real-ip") or "").strip()
        if forwarded:
            return forwarded[:64]
    return str(peer or "unknown")[:64]


def _log_phone_operation(
    store: Any,
    operation: str,
    status: str,
    provider: str,
    elapsed_ms: int,
    *,
    code: str | None = None,
) -> None:
    detail = {"provider": provider, "elapsed_ms": max(0, int(elapsed_ms))}
    if code:
        detail["code"] = code
    print(
        json.dumps(
            {"event": operation, "status": status, **detail},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        store.log(operation, status, "sms_otp", detail)
    except Exception:
        return


def _hydrate_report_for_export(report: dict[str, Any], store: Any) -> dict[str, Any]:
    hydrated = deepcopy(report)
    article_cache: dict[str, dict[str, Any]] = {}

    def article_for(item: dict[str, Any]) -> dict[str, Any]:
        article_id = item.get("article_id")
        if not article_id:
            return {}
        if article_id not in article_cache:
            article_cache[article_id] = store.get_article(article_id) or {}
        return article_cache[article_id]

    def hydrate_item(item: Any) -> Any:
        if not isinstance(item, dict):
            return item
        article = article_for(item)
        if not article:
            return item
        if not item.get("summary"):
            item["summary"] = article.get("summary") or ""
        if not item.get("content"):
            item["content"] = article.get("content") or ""
        if not item.get("full_text"):
            item["full_text"] = article.get("content") or article.get("summary") or item.get("summary") or ""
        if not item.get("published_at"):
            item["published_at"] = article.get("published_at")
        return item

    for item in hydrated.get("sources") or []:
        hydrate_item(item)
    for item in hydrated.get("timeline") or []:
        hydrate_item(item)
    sections = hydrated.get("sections") or {}
    if isinstance(sections, dict):
        for value in sections.values():
            if isinstance(value, list):
                for item in value:
                    hydrate_item(item)
    return hydrated
