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
from personal_news_agent.services.source_registry import SourceRegistryService
from personal_news_agent.services.store import NewsStore


async def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl all configured categories or a selected subset.")
    parser.add_argument("--category", action="append", help="Category to crawl. Can be repeated.")
    parser.add_argument("--per-section-limit", type=int, default=10)
    parser.add_argument("--fetch-articles", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("crawl_results.json"))
    args = parser.parse_args()

    registry = SourceRegistryService(settings.sources_path)
    registry.load()
    store = NewsStore(settings.sqlite_path)
    store.init()
    store.upsert_sources(registry.all_sources())
    scheduler = CrawlScheduler(registry, store)
    categories = args.category or ["economy", "tech", "auto", "game", "anime", "entertainment", "sports"]

    results = []
    for category in categories:
        results.append(await scheduler.crawl_category(category, args.per_section_limit, args.fetch_articles))
    summary = {
        "categories": categories,
        "sections": sum(len(item["results"]) for item in results),
        "saved_articles": sum(result.get("saved", 0) for item in results for result in item["results"]),
        "errors": sum(1 for item in results for result in item["results"] if "error" in result),
    }
    payload = {"summary": summary, "results": results}
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
