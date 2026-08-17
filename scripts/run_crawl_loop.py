from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_news_agent.config import settings
from personal_news_agent.services.crawl import CrawlScheduler
from personal_news_agent.services.crawl_loop import ContinuousCrawlService
from personal_news_agent.services.search_index import ArticleSearchIndex, ElasticsearchArticleIndex
from personal_news_agent.services.source_registry import SourceRegistryService
from personal_news_agent.services.store import NewsStore
from personal_news_agent.services.topic_extraction import TopicExtractionService
from personal_news_agent.services.url_store import CrawlUrlStore, MySQLCrawlUrlStore


async def _wait_while_disabled(service_name: str) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stop.set)
    print(
        json.dumps(
            {
                "status": "disabled",
                "service": service_name,
                "reason": "PNA_AUTOMATED_NEWS_ENABLED=0",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    await stop.wait()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Continuously refresh due portal index pages with a fixed low-concurrency worker pool.")
    parser.add_argument("--category", default=None)
    parser.add_argument("--workers", type=int, default=2, choices=range(1, 5))
    parser.add_argument("--due-limit", type=int, default=20)
    parser.add_argument("--per-section-limit", type=int, default=20)
    parser.add_argument("--fetch-articles", type=int, default=5)
    parser.add_argument("--idle-seconds", type=float, default=30.0)
    parser.add_argument("--extraction-limit", type=int, default=20)
    args = parser.parse_args()

    if not settings.automated_news_enabled:
        await _wait_while_disabled("crawler")
        return

    registry = SourceRegistryService(settings.sources_path)
    registry.load()
    store = NewsStore(settings.sqlite_path)
    store.init()
    store.upsert_sources(registry.all_sources())
    url_store = MySQLCrawlUrlStore.from_settings(settings) or CrawlUrlStore()
    search_index = ElasticsearchArticleIndex.from_settings(settings) or ArticleSearchIndex()
    try:
        url_store.init()
        url_store.sync_sources(registry.all_sources())
    except Exception as exc:
        store.log("crawl_url_store_init", "error", "mysql", {"error": str(exc)})
    try:
        await search_index.ensure_index()
    except Exception as exc:
        store.log("search_index_init", "error", "elasticsearch", {"error": str(exc)})

    service = ContinuousCrawlService(
        CrawlScheduler(registry, store, url_store, search_index),
        store,
        workers=args.workers,
        due_limit=args.due_limit,
        per_section_limit=args.per_section_limit,
        fetch_articles=args.fetch_articles,
        idle_seconds=args.idle_seconds,
        topic_extractor=TopicExtractionService(store),
        extraction_limit=args.extraction_limit,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stop.set)
    print(json.dumps({"status": "started", "workers": args.workers, "category": args.category}, ensure_ascii=False), flush=True)
    await service.run_forever(stop, category=args.category)
    print(json.dumps({"status": "stopped"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
