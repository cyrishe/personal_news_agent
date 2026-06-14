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
from personal_news_agent.services.deep_dive import DeepDiveService
from personal_news_agent.services.search import UnifiedSearchService, external_provider_from_settings
from personal_news_agent.services.search_index import ArticleSearchIndex, ElasticsearchArticleIndex
from personal_news_agent.services.source_registry import SourceRegistryService
from personal_news_agent.services.store import NewsStore


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run query expansion and evidence recall for a deep-dive topic.")
    parser.add_argument("query")
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--breadth", type=int, default=4)
    parser.add_argument("--output", type=Path, default=Path("deep_dive_results.json"))
    args = parser.parse_args()

    registry = SourceRegistryService(settings.sources_path)
    registry.load()
    store = NewsStore(settings.sqlite_path)
    store.init()
    search_index = ElasticsearchArticleIndex.from_settings(settings) or ArticleSearchIndex()
    search = UnifiedSearchService(store, registry, external_provider_from_settings(settings), search_index)
    payload = await DeepDiveService(search).run(
        args.query,
        category_scope=args.category or None,
        source_scope=args.source or None,
        rounds=args.rounds,
        breadth=args.breadth,
    )
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"topic": args.query, "expanded_queries": len(payload["expanded_queries"]), "evidence_groups": len(payload["evidence"])}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
