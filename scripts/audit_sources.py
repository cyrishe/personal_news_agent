from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_news_agent.config import settings
from personal_news_agent.services.source_registry import SourceRegistryService
from personal_news_agent.services.store import NewsStore
from personal_news_agent.services.url_store import CrawlUrlStore, MySQLCrawlUrlStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit source metadata, tags, crawl status, and due sections.")
    parser.add_argument("--category", default=None)
    parser.add_argument("--limit-due", type=int, default=50)
    parser.add_argument("--output", type=Path, default=Path("source_audit_results.json"))
    args = parser.parse_args()

    registry = SourceRegistryService(settings.sources_path)
    registry.load()
    store = NewsStore(settings.sqlite_path)
    store.init()
    store.upsert_sources(registry.all_sources())
    url_store = MySQLCrawlUrlStore.from_settings(settings) or CrawlUrlStore()
    mysql_error = None
    try:
        url_store.init()
        url_store.sync_sources(registry.all_sources())
    except Exception as exc:
        mysql_error = str(exc)

    summary = registry.source_summary()
    due_sections = store.due_sections(category=args.category, limit=args.limit_due)
    due_urls = url_store.list_due(category=args.category, limit=args.limit_due, url_type="section")
    payload = {
        "summary": summary,
        "due": {
            "category": args.category,
            "due_count": sum(1 for item in due_sections if item["due"]),
            "sections": due_sections,
        },
        "mysql_url_store": {
            "ready": url_store.ready,
            "error": mysql_error,
            "due_section_count": len(due_urls),
            "due_sections": due_urls,
        },
        "inventory": store.list_source_inventory(),
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"sources": summary["source_count"], "tags": len(summary["tags"]), "due_count": payload["due"]["due_count"], "mysql_ready": url_store.ready}, ensure_ascii=False))


if __name__ == "__main__":
    main()
