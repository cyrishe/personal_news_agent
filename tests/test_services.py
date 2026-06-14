from pathlib import Path
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from personal_news_agent.core.models import RawArticle, RawArticleLink
from personal_news_agent.services.chat import NewsChatService
from personal_news_agent.services.crawl import CrawlScheduler
from personal_news_agent.services.deep_dive import DeepDiveService
from personal_news_agent.services.events import EventDiscoveryService
from personal_news_agent.services.article_fetch import _parse_published_datetime, _unwrap_search_link
from personal_news_agent.services.native_ingestion import NativeSearchIngestionService
from personal_news_agent.services.personalization import PersonalizationService
from personal_news_agent.services.reports import ReportGenerationService
from personal_news_agent.services.search import UnifiedSearchService
from personal_news_agent.services.source_adapter import ListPageAdapter
from personal_news_agent.services.source_registry import SourceRegistryService
from personal_news_agent.services.store import NewsStore
from personal_news_agent.services.tasks import ScheduledTaskService
from personal_news_agent.services.topic_views import TopicViewService


@pytest.fixture()
def services(tmp_path):
    registry = SourceRegistryService(Path("sources.yaml"))
    registry.load()
    store = NewsStore(tmp_path / "news.db")
    store.init()
    store.upsert_sources(registry.all_sources())
    store.seed_demo_articles()
    search = UnifiedSearchService(store, registry)
    return registry, store, search


def test_search_works_without_external_provider(services):
    _, _, search = services
    results = asyncio.run(search.search("AI Agent", ["tech"], None, None, 10))
    assert results
    assert all(item.category == "tech" for item in results)


def test_search_prefers_elasticsearch_index(services):
    registry, store, _ = services
    search = UnifiedSearchService(store, registry, search_index=FakeArticleIndex())
    results = asyncio.run(search.search("俄乌 农作物", ["politics"], None, None, 10))
    assert results
    assert results[0].origin == "elasticsearch"
    assert results[0].source_id == "people_politics"


def test_native_source_search_encodes_query(services):
    registry, _, _ = services
    source = registry.get_source("hupu")
    fetcher = FakeLinkFetcher()
    results = asyncio.run(ListPageAdapter(source, fetcher=fetcher).search("张雪机车", limit=2))
    assert results
    assert "%E5%BC%A0%E9%9B%AA%E6%9C%BA%E8%BD%A6" in fetcher.urls[0]
    assert "{query" not in fetcher.urls[0]


def test_search_redirect_link_unwraps_targetpage():
    wrapped = "https://search.cctv.com/link_p.php?targetpage=https%3A%2F%2Fsports.cctv.com%2F2026%2F06%2F01%2FARTITest.shtml&point=web"
    assert _unwrap_search_link(wrapped) == "https://sports.cctv.com/2026/06/01/ARTITest.shtml"


def test_parse_published_datetime_assumes_china_timezone_for_naive_time():
    parsed = _parse_published_datetime("2026年05月18日 10:30")
    assert parsed.isoformat() == "2026-05-18T02:30:00+00:00"


def test_native_search_ingestion_fetches_and_indexes_articles(services):
    registry, store, _ = services
    fake_fetcher = FakeLinkFetcher()
    fake_index = FakeWriteIndex()
    service = NativeSearchIngestionService(
        registry,
        store,
        search_index=fake_index,
        adapter_factory=lambda source: ListPageAdapter(source, fetcher=fake_fetcher),
    )

    payload = asyncio.run(
        service.ingest(
            query="张雪机车",
            category_scope=["sports"],
            source_scope=["hupu"],
            max_results=2,
            fetch_articles=1,
        )
    )

    assert payload["discovered_count"] == 1
    assert payload["fetched_count"] == 1
    assert payload["indexed_count"] == 1
    assert fake_index.indexed[0]["title"] == "张雪机车 赛事更新"
    saved = store.search_articles("张雪机车", ["sports"], limit=5)
    assert saved and saved[0]["source_id"] == "hupu"


def test_deep_dive_generates_expansion_queries_and_evidence(services):
    registry, store, _ = services
    search = UnifiedSearchService(store, registry)
    payload = asyncio.run(DeepDiveService(search).run("俄乌战争 农作物", ["politics"], None, rounds=1, breadth=2))
    assert payload["expanded_queries"]
    assert payload["evidence"]
    assert payload["strategy"]["llm_planner"].startswith("预留")


def test_event_discovery_generates_required_fields(services):
    _, store, _ = services
    clusters = EventDiscoveryService(store).discover(category="auto")
    assert clusters
    cluster = clusters[0]
    assert cluster.title
    assert cluster.category == "auto"
    assert cluster.article_count >= 1
    assert cluster.source_count >= 1
    assert cluster.hot_score > 0


def test_personalized_feed_changes_with_profile(services):
    registry, store, _ = services
    feed = PersonalizationService(store, registry)
    default_first = feed.feed("default", limit=1)[0]
    store.save_profile(
        {
            "user_id": "sports_user",
            "interests": ["球队"],
            "negative_interests": [],
            "preferred_categories": ["sports"],
            "preferred_sources": [],
            "output_style": "concise",
        }
    )
    sports_first = feed.feed("sports_user", limit=1)[0]
    assert default_first.category != sports_first.category
    assert sports_first.category == "sports"
    assert "sports" in sports_first.matched_profile_terms
    assert sports_first.source_tags


def test_personalized_feed_covers_multiple_preferred_categories(services):
    registry, store, _ = services
    store.save_profile(
        {
            "user_id": "politics_sports_user",
            "self_description": "关心时政和体育，重点看 NBA 和粮食安全。",
            "interests": ["NBA", "粮食"],
            "negative_interests": [],
            "preferred_categories": ["politics", "sports"],
            "preferred_sources": [],
            "output_style": "简洁分析型",
        }
    )

    items = PersonalizationService(store, registry).feed("politics_sports_user", limit=6)
    categories = [item.category for item in items]
    assert "politics" in categories
    assert "sports" in categories
    assert any("politics" in item.source_tags for item in items if item.category == "politics")
    assert any("sports" in item.matched_profile_terms for item in items)


def test_source_due_plan_uses_crawl_metadata(services):
    registry, store, _ = services
    scheduler = CrawlScheduler(registry, store)
    plan = scheduler.due_plan(category="tech", limit=5)
    assert plan["sections"]
    assert plan["due_count"] >= 1
    first = plan["sections"][0]
    assert first["category"] == "tech"
    assert first["source_tags"]
    store.mark_section_crawled(first["source_id"], first["section_key"])
    updated = scheduler.due_plan(category="tech", limit=5)
    same = [item for item in updated["sections"] if item["source_id"] == first["source_id"] and item["section_key"] == first["section_key"]]
    assert same and same[0]["due"] is False


def test_chat_resolves_second_article_followup(services):
    _, store, search = services
    chat = NewsChatService(store, search)
    first = asyncio.run(chat.chat("conv_test", "今天游戏圈有什么新闻？"))
    assert len(first.recommendations) >= 2

    second = asyncio.run(chat.chat("conv_test", "第二条展开说说。"))
    assert second.context_relation == "follow_up"
    assert second.focus_object is not None
    assert second.focus_object.type == "article"
    assert second.focus_object.ordinal == 2
    assert "previous_recommendation_list" in second.required_context_items


def test_report_contains_timeline_and_required_sections(services):
    _, store, search = services
    reports = ReportGenerationService(store, search)
    report = asyncio.run(reports.generate("default", "新能源汽车价格战", ["auto", "economy"]))

    assert report.timeline
    assert "一、结论摘要" in report.sections
    assert "三、关键时间线" in report.sections
    assert "八、来源列表与不确定性说明" in report.sections
    with store.connect() as conn:
        operations = [row["operation"] for row in conn.execute("SELECT operation FROM operation_logs").fetchall()]
    assert "news_search" in operations
    assert "report_generation" in operations


def test_topic_view_builds_event_line_and_relation_graph(services):
    _, store, search = services
    payload = asyncio.run(TopicViewService(store, search).build("新能源汽车价格战", ["auto", "economy"], None, max_articles=8))

    assert payload["topic"]["title"] == "新能源汽车价格战"
    assert payload["event_line"]["items"]
    assert payload["relation_graph"]["nodes"]
    assert payload["relation_graph"]["edges"]
    assert payload["relation_graph"]["nodes"][0]["type"] == "topic"


def test_scheduled_task_runs_and_generates_report(services):
    _, store, search = services
    reports = ReportGenerationService(store, search)
    tasks = ScheduledTaskService(store, reports)
    task = tasks.create_task(
        {
            "user_id": "default",
            "task_type": "daily_digest",
            "schedule": "0 21 * * *",
            "category_scope": ["tech", "game", "auto"],
            "topics": ["AI", "任天堂", "新能源汽车"],
            "output_style": "简洁分析型",
        }
    )
    assert task["next_run_at"]
    result = asyncio.run(tasks.run_task(task["id"]))
    assert result["status"] == "ok"
    assert result["report_id"].startswith("rpt_")
    assert result["notification"]["target_id"] == result["report_id"]
    notifications = store.list_notifications("default")
    assert notifications
    assert notifications[0]["payload"]["task_id"] == task["id"]


def test_due_tasks_create_notifications(services):
    _, store, search = services
    reports = ReportGenerationService(store, search)
    tasks = ScheduledTaskService(store, reports)
    task = tasks.create_task(
        {
            "user_id": "due_user",
            "task_type": "topic_tracking",
            "schedule": "*/20 * * * *",
            "category_scope": ["sports"],
            "topics": ["张雪机车"],
            "delivery_channel": "browser",
        }
    )
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with store.connect() as conn:
        conn.execute("UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ?", (past, task["id"]))

    result = asyncio.run(tasks.run_due_tasks("due_user"))
    assert result["ran_count"] == 1
    assert result["notifications"][0]["delivery_channel"] == "browser"
    assert store.get_task(task["id"])["next_run_at"] > past


class FakeArticleIndex:
    configured = True

    async def search(self, query, category_scope=None, source_scope=None, limit=20):
        return [
            {
                "id": "art_es_seed",
                "source_id": "people_politics",
                "title": "俄乌战争影响农作物出口",
                "url": "https://example.local/es",
                "summary": "粮食安全和农作物价格受到关注。",
                "category": "politics",
                "published_at": None,
            }
        ][:limit]


class FakeLinkFetcher:
    def __init__(self):
        self.urls = []

    async def list_links(self, source_id, section_key, url, limit=30, allowed_domains=None):
        self.urls.append(url)
        return [
            RawArticleLink(
                source_id=source_id,
                section_key=section_key,
                title="张雪机车 赛事更新",
                url="https://bbs.hupu.com/639652293.html",
            )
        ][:limit]

    async def fetch_article(self, source_id, url):
        return RawArticle(
            source_id=source_id,
            url=url,
            title="张雪机车 赛事更新",
            summary="张雪机车在 WSBK 赛事中继续受到关注。",
            content="张雪机车在 WSBK 赛事中继续受到关注，车队成绩、商业合作和舆论讨论同步升温。",
        )


class FakeWriteIndex:
    configured = True

    def __init__(self):
        self.indexed = []

    async def ensure_index(self):
        return {"configured": True, "ready": True}

    async def index_article(self, article):
        self.indexed.append(article)

    async def search(self, query, category_scope=None, source_scope=None, limit=20):
        return []
