from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from personal_news_agent.config import settings
from personal_news_agent.core.categories import CATEGORIES
from personal_news_agent.core.models import TimeRange
from personal_news_agent.services.auth import AuthError, AuthService
from personal_news_agent.services.chat import NewsChatService
from personal_news_agent.services.crawl import CrawlScheduler
from personal_news_agent.services.deep_dive import DeepDiveService
from personal_news_agent.services.events import EventDiscoveryService
from personal_news_agent.services.model_config import public_model_options
from personal_news_agent.services.native_ingestion import NativeSearchIngestionService
from personal_news_agent.services.onboarding import OnboardingService
from personal_news_agent.services.personalization import PersonalizationService
from personal_news_agent.services.reports import ReportGenerationService
from personal_news_agent.services.search import UnifiedSearchService, external_provider_from_settings
from personal_news_agent.services.search_index import ArticleSearchIndex, ElasticsearchArticleIndex
from personal_news_agent.services.source_registry import SourceRegistryError, SourceRegistryService
from personal_news_agent.services.store import NewsStore
from personal_news_agent.services.tasks import ScheduledTaskService
from personal_news_agent.services.topic_views import TopicViewService
from personal_news_agent.services.url_store import CrawlUrlStore, MySQLCrawlUrlStore


class SearchRequest(BaseModel):
    query: str
    category_scope: list[str] | None = None
    source_scope: list[str] | None = None
    time_range: str | None = "7d"
    max_results: int = Field(default=20, ge=1, le=100)


class DeepDiveRequest(BaseModel):
    query: str
    category_scope: list[str] | None = None
    source_scope: list[str] | None = None
    rounds: int = Field(default=2, ge=1, le=4)
    breadth: int = Field(default=4, ge=1, le=8)


class NativeSearchIngestRequest(BaseModel):
    query: str
    category_scope: list[str] | None = None
    source_scope: list[str] | None = None
    max_results: int = Field(default=20, ge=1, le=100)
    fetch_articles: int = Field(default=10, ge=0, le=50)
    follow_depth: int = Field(default=0, ge=0, le=1)
    follow_limit_per_article: int = Field(default=2, ge=0, le=5)


class TopicViewRequest(BaseModel):
    topic: str
    category_scope: list[str] | None = None
    source_scope: list[str] | None = None
    max_articles: int = Field(default=16, ge=1, le=50)


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str
    topic: str | None = None
    category_scope: list[str] | None = None
    use_llm: bool = False


class ReportRequest(BaseModel):
    user_id: str = "default"
    topic: str
    category_scope: list[str] = []
    time_range: str = "30d"
    report_type: str = "timeline_analysis"


class ProfileRequest(BaseModel):
    user_id: str = "default"
    interests: list[str] = []
    negative_interests: list[str] = []
    preferred_categories: list[str] = []
    preferred_sources: list[str] = []
    output_style: str = "concise"


class FeedbackRequest(BaseModel):
    user_id: str = "default"
    target_type: str
    target_id: str
    feedback_type: str


class TaskRequest(BaseModel):
    user_id: str = "default"
    task_type: str
    schedule: str
    category_scope: list[str] = []
    source_scope: list[str] = []
    topics: list[str] = []
    output_style: str | None = None
    delivery_channel: str = "in_app"


class DueTasksRequest(BaseModel):
    user_id: str = "default"
    limit: int = Field(default=10, ge=1, le=50)


class NotificationReadRequest(BaseModel):
    user_id: str = "default"


class DueCrawlRequest(BaseModel):
    category: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    per_section_limit: int = Field(default=10, ge=1, le=50)
    fetch_articles: int = Field(default=1, ge=0, le=10)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=6, max_length=128)
    confirm_password: str = Field(min_length=6, max_length=128)
    real_name: str = Field(min_length=2, max_length=40)
    mobile: str = Field(min_length=11, max_length=11)
    id_card: str | None = Field(default=None, min_length=15, max_length=18)


class LoginRequest(BaseModel):
    username: str
    password: str


class OnboardingRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    user_id: str
    display_name: str | None = None
    self_description: str = Field(default="", max_length=500)
    age: int | None = None
    gender: str = "不透露"
    zodiac: str = "不透露"
    preferred_categories: list[str] = []
    watch_keywords: list[str] = []
    negative_keywords: list[str] = []
    model_key: str = "yuanrong-personal-assistant"
    output_style: str = "简洁分析型"


def create_app() -> FastAPI:
    registry = SourceRegistryService(settings.sources_path)
    store = NewsStore(settings.sqlite_path)
    search_index = ElasticsearchArticleIndex.from_settings(settings) or ArticleSearchIndex()
    url_store = MySQLCrawlUrlStore.from_settings(settings) or CrawlUrlStore()
    search_service = UnifiedSearchService(store, registry, external_provider_from_settings(settings), search_index)
    events = EventDiscoveryService(store)
    native_ingestion = NativeSearchIngestionService(registry, store, url_store, search_index)
    topic_views = TopicViewService(store, search_service)
    deep_dive = DeepDiveService(search_service)
    app = FastAPI(title=settings.app_name)
    state: dict[str, Any] = {
        "registry": registry,
        "store": store,
        "url_store": url_store,
        "search_index": search_index,
        "search": search_service,
        "native_ingestion": native_ingestion,
        "topic_views": topic_views,
        "deep_dive": deep_dive,
        "events": events,
        "auth": AuthService(store, settings),
        "onboarding": OnboardingService(store, settings),
        "feed": PersonalizationService(store, registry),
        "chat": NewsChatService(store, search_service, native_ingestion=native_ingestion, deep_dive=deep_dive, topic_views=topic_views),
        "reports": ReportGenerationService(store, search_service),
    }
    state["tasks"] = ScheduledTaskService(store, state["reports"])
    state["crawl"] = CrawlScheduler(registry, store, url_store, search_index)
    app.state.services = state

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.on_event("startup")
    async def startup() -> None:
        try:
            registry.load()
        except SourceRegistryError:
            raise
        store.init()
        store.upsert_sources(registry.all_sources())
        try:
            url_store.init()
            url_store.sync_sources(registry.all_sources())
        except Exception as exc:
            store.log("crawl_url_store_init", "error", "mysql", {"error": str(exc)})
        try:
            await search_index.ensure_index()
        except Exception as exc:
            store.log("search_index_init", "error", "elasticsearch", {"error": str(exc)})
        if settings.seed_demo_data:
            store.seed_demo_articles()
        events.discover(limit=20)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "web.html")

    @app.get("/web")
    async def web_app() -> FileResponse:
        return FileResponse(static_dir / "web.html")

    @app.get("/auth")
    async def auth_app() -> FileResponse:
        return FileResponse(static_dir / "auth.html")

    @app.get("/mobile")
    async def mobile_app() -> FileResponse:
        return FileResponse(static_dir / "mobile.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "categories": CATEGORIES, "source_count": len(registry.all_sources())}

    @app.get("/api/models")
    async def models() -> dict[str, Any]:
        return {
            "items": public_model_options(),
            "default_model": settings.llm_default_model,
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
        return state["crawl"].due_plan(category=category, limit=limit)

    @app.get("/api/crawl/urls/due")
    async def crawl_due_urls(category: str | None = None, limit: int = Query(default=50, ge=1, le=200), url_type: str | None = "article") -> dict[str, Any]:
        return {"items": url_store.list_due(category=category, limit=limit, url_type=url_type), "mysql_ready": url_store.ready}

    @app.post("/api/crawl/due")
    async def crawl_due(payload: DueCrawlRequest) -> dict[str, Any]:
        return await state["crawl"].crawl_due(
            category=payload.category,
            limit=payload.limit,
            per_section_limit=payload.per_section_limit,
            fetch_articles=payload.fetch_articles,
        )

    @app.get("/api/feed")
    async def feed(category: str | None = None, limit: int = Query(default=20, ge=1, le=100), user_id: str = "default") -> dict[str, Any]:
        return {"items": state["feed"].feed(user_id=user_id, category=category, limit=limit)}

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
                "mobile": _mask_mobile(user.get("mobile") or ""),
                "realname_verified": bool(user.get("realname_verified")),
                "realname_provider": user.get("realname_provider"),
                "assistant_prompt": user.get("assistant_prompt"),
            } if user else None,
            "profile": profile,
        }

    @app.post("/api/auth/register")
    async def register(payload: RegisterRequest) -> dict[str, Any]:
        try:
            return state["auth"].register_with_realname(
                username=payload.username,
                password=payload.password,
                confirm_password=payload.confirm_password,
                real_name=payload.real_name,
                mobile=payload.mobile,
                id_card=payload.id_card,
            )
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/auth/login")
    async def login(payload: LoginRequest) -> dict[str, Any]:
        try:
            return state["auth"].login(payload.username, payload.password)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.get("/api/auth/realname/status")
    async def realname_status() -> dict[str, Any]:
        return state["auth"].realname.status()

    @app.get("/api/onboarding/options")
    async def onboarding_options() -> dict[str, Any]:
        return state["onboarding"].options()

    @app.post("/api/onboarding/complete")
    async def onboarding_complete(payload: OnboardingRequest) -> dict[str, Any]:
        try:
            return state["onboarding"].complete(payload.user_id, payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/auth/wechat/status")
    async def wechat_status() -> dict[str, Any]:
        return state["auth"].wechat_status()

    @app.get("/api/auth/wechat/login-url")
    async def wechat_login_url(mode: str | None = None, state_param: str | None = Query(default=None, alias="state"), redirect_uri: str | None = None) -> dict[str, Any]:
        try:
            return state["auth"].wechat_login_url(mode=mode, state=state_param, redirect_uri=redirect_uri)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/auth/wechat/callback")
    async def wechat_callback(code: str, state_param: str | None = Query(default=None, alias="state")) -> dict[str, Any]:
        try:
            return await state["auth"].wechat_callback(code=code, state=state_param)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/feedback")
    async def feedback(payload: FeedbackRequest) -> dict[str, Any]:
        store.save_feedback(payload.user_id, payload.target_type, payload.target_id, payload.feedback_type)
        return {"status": "ok"}

    @app.post("/api/news/search")
    async def search(payload: SearchRequest) -> dict[str, Any]:
        time_range = _parse_range(payload.time_range)
        results = await search_service.search(payload.query, payload.category_scope, payload.source_scope, time_range, payload.max_results)
        return {"items": results}

    @app.post("/api/news/deep-dive")
    async def deep_dive(payload: DeepDiveRequest) -> dict[str, Any]:
        return await state["deep_dive"].run(
            payload.query,
            category_scope=payload.category_scope,
            source_scope=payload.source_scope,
            rounds=payload.rounds,
            breadth=payload.breadth,
        )

    @app.post("/api/news/search/ingest")
    async def native_search_ingest(payload: NativeSearchIngestRequest) -> dict[str, Any]:
        return await state["native_ingestion"].ingest(
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
        return await state["topic_views"].build(
            topic=payload.topic,
            category_scope=payload.category_scope,
            source_scope=payload.source_scope,
            max_articles=payload.max_articles,
        )

    @app.get("/api/news/search/backend")
    async def search_backend() -> dict[str, Any]:
        return {
            "configured_backend": settings.search_backend,
            "local_backend": "sqlite_fts",
            "primary_recall_backend": "elasticsearch" if search_index.configured else "sqlite_fts",
            "external_provider": settings.external_search_provider,
            "external_configured": bool(settings.bing_search_key) if settings.external_search_provider == "bing" else False,
            "elasticsearch": await search_index.health(),
            "crawl_url_store": {
                "backend": settings.crawl_url_backend,
                "mysql_configured": bool(settings.crawl_database_url),
                "mysql_ready": url_store.ready,
            },
        }

    @app.get("/api/events")
    async def list_events(category: str | None = None, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        clusters = events.discover(category=category, limit=limit)
        return {"items": clusters}

    @app.post("/api/chat")
    async def chat(payload: ChatRequest) -> Any:
        return await state["chat"].chat(payload.conversation_id, payload.message, payload.topic, payload.category_scope, payload.use_llm)

    @app.post("/api/chat/stream")
    async def chat_stream(payload: ChatRequest) -> StreamingResponse:
        async def event_stream():
            async for event in state["chat"].chat_events(payload.conversation_id, payload.message, payload.topic, payload.category_scope, payload.use_llm):
                event_type = event.get("type", "message")
                data = json.dumps(event, ensure_ascii=False, default=str)
                yield f"event: {event_type}\ndata: {data}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/reports")
    async def reports(payload: ReportRequest) -> Any:
        return await state["reports"].generate(payload.user_id, payload.topic, payload.category_scope, payload.time_range, payload.report_type)

    @app.post("/api/tasks")
    async def create_task(payload: TaskRequest) -> dict[str, Any]:
        try:
            return state["tasks"].create_task(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/tasks")
    async def list_tasks(user_id: str = "default", limit: int = Query(default=50, ge=1, le=100)) -> dict[str, Any]:
        return {"items": state["tasks"].list_tasks(user_id=user_id, limit=limit)}

    @app.post("/api/tasks/due/run")
    async def run_due_tasks(payload: DueTasksRequest) -> dict[str, Any]:
        return await state["tasks"].run_due_tasks(user_id=payload.user_id, limit=payload.limit)

    @app.post("/api/tasks/{task_id}/run")
    async def run_task(task_id: str) -> dict[str, Any]:
        try:
            return await state["tasks"].run_task(task_id)
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
            return await state["crawl"].crawl_category(category)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


def _parse_range(value: str | None) -> TimeRange | None:
    if not value:
        return None
    if value.endswith("d") and value[:-1].isdigit():
        return TimeRange(days=int(value[:-1]))
    return TimeRange(days=7)


def _mask_mobile(value: str) -> str:
    return value[:3] + "****" + value[-4:] if len(value) == 11 else value


app = create_app()
