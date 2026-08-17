from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from personal_news_agent.api.routes import register_routes
from personal_news_agent.config import Settings, settings
from personal_news_agent.services.factory import build_services
from personal_news_agent.services.source_registry import SourceRegistryError
from claude_code_backend import create_local_agent_router

def create_app(app_settings: Settings | None = None) -> FastAPI:
    runtime_settings = app_settings or settings
    services = build_services(runtime_settings)
    app = FastAPI(title=runtime_settings.app_name)
    app.state.services = services
    app.include_router(create_local_agent_router(services["local_agent"]))

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    register_routes(app, services, static_dir, runtime_settings)

    @app.on_event("startup")
    async def startup() -> None:
        registry = services["registry"]
        store = services["store"]
        url_store = services["url_store"]
        search_index = services["search_index"]
        events = services["events"]

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
        if runtime_settings.seed_demo_data:
            store.seed_demo_articles()
        services["topic_agent"].seed_system_topics()
        events.discover(limit=20)
        if runtime_settings.automated_news_enabled:
            app.state.trending_topic_task = asyncio.create_task(
                _trending_topic_loop(services, runtime_settings.trending_topic_refresh_seconds)
            )
        if runtime_settings.automated_news_enabled and runtime_settings.background_crawl_enabled:
            app.state.background_crawl_task = asyncio.create_task(
                _background_crawl_loop(services, runtime_settings.background_crawl_interval_seconds)
            )

    @app.on_event("shutdown")
    async def shutdown() -> None:
        for task_name in ("background_crawl_task", "trending_topic_task"):
            task = getattr(app.state, task_name, None)
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    return app


app = create_app()


async def _background_crawl_loop(services: dict, interval_seconds: int) -> None:
    store = services["store"]
    crawl = services["crawl"]
    events = services["events"]
    interval = max(10, int(interval_seconds or 10))
    await asyncio.sleep(5)
    while True:
        try:
            result = await crawl.crawl_due(limit=20, per_section_limit=8, fetch_articles=1)
            clusters = events.discover(limit=20)
            store.log(
                "background_crawl",
                "ok",
                "crawl_due",
                {"saved_articles": result.get("saved_articles", 0), "planned_sections": result.get("planned_sections", 0), "cluster_count": len(clusters)},
            )
        except Exception as exc:
            store.log("background_crawl", "error", "crawl_due", {"error": str(exc)})
        await asyncio.sleep(interval)


async def _trending_topic_loop(services: dict, interval_seconds: int) -> None:
    interval = max(60, int(interval_seconds or 900))
    await asyncio.sleep(8)
    while True:
        try:
            await services["trending_topics"].recommend(
                "default",
                limit=12,
                window_hours=24,
                refresh_window_hours=6,
                use_llm=True,
            )
        except Exception as exc:
            services["store"].log("trending_topic_refresh", "error", "default", {"error": str(exc)})
        await asyncio.sleep(interval)
