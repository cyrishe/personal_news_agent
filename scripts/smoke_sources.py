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
from personal_news_agent.services.source_adapter import ListPageAdapter
from personal_news_agent.services.source_registry import SourceRegistryService


async def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke test configured news sources.")
    parser.add_argument("--category", action="append", help="Limit to one or more categories.")
    parser.add_argument("--limit-sources", type=int, default=0, help="Maximum sources to test. 0 means all.")
    parser.add_argument("--links", type=int, default=5, help="Minimum links expected per crawlable source.")
    parser.add_argument("--fetch", action="store_true", help="Fetch the first article for each source.")
    parser.add_argument("--output", type=Path, default=Path("source_smoke_results.json"))
    args = parser.parse_args()

    registry = SourceRegistryService(settings.sources_path)
    registry.load()
    sources = registry.all_sources()
    if args.category:
        allowed = set(args.category)
        sources = [source for source in sources if set(source.categories) & allowed]
    if args.limit_sources:
        sources = sources[: args.limit_sources]

    results = []
    for source in sources:
        if not source.crawl_enabled:
            results.append({"source_id": source.source_id, "status": "skipped", "reason": "crawl_disabled"})
            continue
        adapter = ListPageAdapter(source)
        source_result = {"source_id": source.source_id, "status": "error", "sections": []}
        for section in source.sections:
            if not section.crawl_enabled:
                continue
            section_result = {"section_key": section.key, "url": section.url, "link_count": 0, "fetch_ok": False}
            try:
                links = await adapter.crawl_section(section.key, limit=max(args.links, 1))
                section_result["link_count"] = len(links)
                if args.fetch and links:
                    fetch_errors = []
                    for link in links[: min(3, len(links))]:
                        try:
                            raw = await adapter.fetch_article(link.url)
                            section_result["fetch_ok"] = bool(raw.title and raw.content)
                            section_result["sample_title"] = raw.title[:120]
                            if section_result["fetch_ok"]:
                                break
                        except Exception as exc:
                            fetch_errors.append(f"{link.url}: {exc}")
                    if fetch_errors and not section_result["fetch_ok"]:
                        section_result["fetch_error"] = " | ".join(fetch_errors)
                section_result["status"] = "ok" if len(links) >= args.links and (not args.fetch or section_result["fetch_ok"]) else "weak"
            except Exception as exc:
                section_result["status"] = "error"
                section_result["error"] = str(exc)
            source_result["sections"].append(section_result)
        statuses = [section["status"] for section in source_result["sections"]]
        if statuses and all(status == "ok" for status in statuses):
            source_result["status"] = "ok"
        elif statuses and any(status in {"ok", "weak"} for status in statuses):
            source_result["status"] = "weak"
        results.append(source_result)

    summary = {
        "total_sources": len(results),
        "ok_sources": sum(1 for item in results if item["status"] == "ok"),
        "weak_sources": sum(1 for item in results if item["status"] == "weak"),
        "error_sources": sum(1 for item in results if item["status"] == "error"),
        "skipped_sources": sum(1 for item in results if item["status"] == "skipped"),
    }
    payload = {"summary": summary, "results": results}
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
