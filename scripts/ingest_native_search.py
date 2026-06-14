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
from personal_news_agent.services.native_ingestion import NativeSearchIngestionService
from personal_news_agent.services.search_index import ArticleSearchIndex, ElasticsearchArticleIndex
from personal_news_agent.services.source_registry import SourceRegistryService
from personal_news_agent.services.store import NewsStore
from personal_news_agent.services.url_store import CrawlUrlStore, MySQLCrawlUrlStore


async def main() -> None:
    parser = argparse.ArgumentParser(description="Search native source paths/APIs, follow result links, fetch articles, and index them.")
    parser.add_argument("--query", required=True)
    parser.add_argument("--category", action="append", dest="categories", default=None)
    parser.add_argument("--source", action="append", dest="sources", default=None)
    parser.add_argument("--max-results", type=int, default=20)
    parser.add_argument("--fetch-articles", type=int, default=10)
    parser.add_argument("--follow-depth", type=int, default=0)
    parser.add_argument("--follow-limit-per-article", type=int, default=2)
    parser.add_argument("--output", type=Path, default=Path("native_search_ingest_results.json"))
    args = parser.parse_args()

    registry = SourceRegistryService(settings.sources_path)
    registry.load()
    store = NewsStore(settings.sqlite_path)
    store.init()
    store.upsert_sources(registry.all_sources())
    url_store = MySQLCrawlUrlStore.from_settings(settings) or CrawlUrlStore()
    search_index = ElasticsearchArticleIndex.from_settings(settings) or ArticleSearchIndex()
    mysql_error = None
    try:
        url_store.init()
        url_store.sync_sources(registry.all_sources())
    except Exception as exc:
        mysql_error = str(exc)
        store.log("crawl_url_store_init", "error", "mysql", {"error": str(exc)})
    try:
        await search_index.ensure_index()
    except Exception as exc:
        store.log("search_index_init", "error", "elasticsearch", {"error": str(exc)})

    service = NativeSearchIngestionService(registry, store, url_store, search_index)
    payload = await service.ingest(
        query=args.query,
        category_scope=args.categories,
        source_scope=args.sources,
        max_results=args.max_results,
        fetch_articles=args.fetch_articles,
        follow_depth=args.follow_depth,
        follow_limit_per_article=args.follow_limit_per_article,
    )
    payload["mysql_error"] = mysql_error
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        json.dumps(
            {
                "query": args.query,
                "discovered": payload["discovered_count"],
                "fetched": payload["fetched_count"],
                "indexed": payload["indexed_count"],
                "errors": len(payload["errors"]),
                "mysql_ready": payload["mysql_ready"],
                "elasticsearch_configured": payload["elasticsearch_configured"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
