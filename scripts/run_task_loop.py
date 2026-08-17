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
from personal_news_agent.services.factory import build_services
from personal_news_agent.services.source_registry import SourceRegistryError


async def _wait_while_disabled(service_name: str) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stop.set)
    print(
        json.dumps(
            {
                "event": "disabled",
                "service": service_name,
                "reason": "PNA_AUTOMATED_NEWS_ENABLED=0",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    await stop.wait()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Continuously run due per-user scheduled tasks.")
    parser.add_argument("--user-id", default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--idle-seconds", type=float, default=30.0)
    args = parser.parse_args()

    if not settings.automated_news_enabled:
        await _wait_while_disabled("scheduled_tasks")
        return

    services = build_services(settings)
    store = services["store"]
    registry = services["registry"]
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
    if settings.seed_demo_data:
        store.seed_demo_articles()
    services["topic_agent"].seed_system_topics()
    events.discover(limit=20)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stop.set)

    print(json.dumps({"event": "task_loop_started", "user_id": args.user_id, "limit": args.limit}, ensure_ascii=False), flush=True)
    while not stop.is_set():
        try:
            result = await services["tasks"].run_due_tasks(user_id=args.user_id, limit=args.limit)
            print(
                json.dumps(
                    {
                        "event": "task_round",
                        "status": "ok",
                        "user_id": args.user_id,
                        "ran_count": result.get("ran_count", 0),
                        "notifications": len(result.get("notifications") or []),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
                flush=True,
            )
        except Exception as exc:
            store.log("scheduled_task_loop", "error", args.user_id or "all", {"error": str(exc)})
            print(json.dumps({"event": "task_round", "status": "error", "error": str(exc)}, ensure_ascii=False), flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(1.0, args.idle_seconds))
        except asyncio.TimeoutError:
            pass
    print(json.dumps({"event": "task_loop_stopped"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
