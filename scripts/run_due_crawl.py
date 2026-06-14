from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_news_agent.config import settings
from personal_news_agent.services.crawl import CrawlScheduler
from personal_news_agent.services.search_index import ArticleSearchIndex, ElasticsearchArticleIndex
from personal_news_agent.services.source_registry import SourceRegistryService
from personal_news_agent.services.store import NewsStore
from personal_news_agent.services.url_store import CrawlUrlStore, MySQLCrawlUrlStore


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run due source sections according to crawl_interval_minutes.")
    parser.add_argument("--category", default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--per-section-limit", type=int, default=10)
    parser.add_argument("--fetch-articles", type=int, default=1)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("due_crawl_results.json"))
    args = parser.parse_args()

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
    scheduler = CrawlScheduler(registry, store, url_store, search_index)

    payload = scheduler.due_plan(category=args.category, limit=args.limit) if args.plan_only else await scheduler.crawl_due(
        category=args.category,
        limit=args.limit,
        per_section_limit=args.per_section_limit,
        fetch_articles=args.fetch_articles,
    )
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    summary = {
        "mode": "plan" if args.plan_only else "crawl",
        "category": args.category,
        "planned": payload.get("due_count", payload.get("planned_sections")),
        "saved_articles": payload.get("saved_articles", 0),
        "errors": payload.get("errors", 0),
        "mysql_ready": url_store.ready,
        "elasticsearch_configured": search_index.configured,
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
