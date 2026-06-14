from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_news_agent.config import settings
from personal_news_agent.services.personalization import PersonalizationService
from personal_news_agent.services.source_registry import SourceRegistryService
from personal_news_agent.services.store import NewsStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview personalized feed scoring for a user/profile.")
    parser.add_argument("--user-id", default="preview_user")
    parser.add_argument("--category", default=None)
    parser.add_argument("--interest", action="append", default=[])
    parser.add_argument("--negative", action="append", default=[])
    parser.add_argument("--preferred-category", action="append", default=[])
    parser.add_argument("--self-description", default="")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--output", type=Path, default=Path("feed_preview_results.json"))
    args = parser.parse_args()

    registry = SourceRegistryService(settings.sources_path)
    registry.load()
    store = NewsStore(settings.sqlite_path)
    store.init()
    store.upsert_sources(registry.all_sources())
    store.seed_demo_articles()
    if args.interest or args.preferred_category or args.self_description:
        store.save_profile(
            {
                "user_id": args.user_id,
                "self_description": args.self_description,
                "interests": args.interest,
                "negative_interests": args.negative,
                "preferred_categories": args.preferred_category,
                "preferred_sources": [],
                "output_style": "简洁分析型",
            }
        )

    feed = PersonalizationService(store, registry).feed(args.user_id, category=args.category, limit=args.limit)
    payload = {"user_id": args.user_id, "items": [item.model_dump(mode="json") for item in feed]}
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"user_id": args.user_id, "items": len(feed), "top": feed[0].title if feed else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
