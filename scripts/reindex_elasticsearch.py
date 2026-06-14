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
from personal_news_agent.services.search_index import ElasticsearchArticleIndex
from personal_news_agent.services.store import NewsStore


async def main() -> None:
    parser = argparse.ArgumentParser(description="Reindex local articles into Elasticsearch.")
    parser.add_argument("--category", default=None)
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()

    index = ElasticsearchArticleIndex.from_settings(settings)
    if not index:
        raise SystemExit("ELASTICSEARCH_URL is not configured")
    await index.ensure_index()

    store = NewsStore(settings.sqlite_path)
    store.init()
    rows = store.list_articles(category=args.category, limit=args.limit)
    indexed = 0
    for row in rows:
        await index.index_article(row)
        indexed += 1
    print(json.dumps({"indexed": indexed, "index": settings.elasticsearch_index}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
