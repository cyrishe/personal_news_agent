from pathlib import Path
import asyncio
import json
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO

import httpx
import pytest
from bs4 import BeautifulSoup

from personal_news_agent.config import Settings
from personal_news_agent.core.models import NormalizedArticle, RawArticle, RawArticleLink, RawSearchResult, SearchResult, TimeRange
from personal_news_agent.core.text import content_hash, stable_id
from personal_news_agent.services.chat import (
    NewsChatService,
    SearchQueryPlan,
    _filter_by_time,
    _filter_research_results,
    _filter_related_runtime_results,
    _rank_for_chat,
    _related_base_query,
    _related_context_query,
    _related_search_answer,
    _report_skill_answer,
)
from personal_news_agent.services.cc_runtime import (
    CCRuntimeOrchestrator,
    CCRuntimeResult,
    LOCAL_TOOL_NAME,
    NEWS_DAILY_BRIEF_SKILL_NAME,
    NEWS_CONVERSATION_RESEARCH_SKILL_NAME,
    NEWS_RELATED_EXPLORATION_SKILL_NAME,
    NEWS_SOURCE_AUDIT_SKILL_NAME,
    NEWS_TOPIC_REPORT_SKILL_NAME,
    SCHEDULED_NEWS_TASK_SKILL_NAME,
    RuntimeSearchContext,
    WEB_TOOL_NAME,
    _system_prompt,
)
from personal_news_agent.services.crawl import CrawlScheduler
from personal_news_agent.services.deep_dive import DeepDiveService
from personal_news_agent.services.events import EventDiscoveryService, _cluster_title
from personal_news_agent.services.factcheck import FactCheckService, _merge_search_results, _normalize_payload
from personal_news_agent.services.article_fetch import ArticleFetchService, _extract_published_at, _parse_published_datetime, _unwrap_search_link
from personal_news_agent.services.chat_understanding import (
    categories_for_message,
    query_from_message,
)
from personal_news_agent.services.content_moderation import ContentModerationError, TextModerationPlusService
from personal_news_agent.services.native_ingestion import NativeSearchIngestionService
from personal_news_agent.services.personalization import PersonalizationService
from personal_news_agent.services.report_export import export_report
from personal_news_agent.services.reports import ReportGenerationService, _brief_story_summary, _fallback_brief_sections, _filter_conversation_evidence_by_topic
from personal_news_agent.services.search import (
    ExternalSearchProvider,
    TavilySearchProvider,
    UnifiedSearchService,
    _relevance_terms,
    external_provider_from_settings,
    search_result_matches_subject,
    search_result_matches_terms,
)
from personal_news_agent.services.source_adapter import ListPageAdapter
from personal_news_agent.services.source_adapter import _record_value
from personal_news_agent.services.source_registry import SourceRegistryService
from personal_news_agent.services.store import NewsStore
from personal_news_agent.services.tasks import ScheduledTaskService, parse_schedule_command
from personal_news_agent.services.topic_agent import TopicAgentService
from personal_news_agent.services.topic_summary import TopicSummaryOutput, TopicSummaryService
from personal_news_agent.services.topic_views import TopicViewService
from personal_news_agent.skills.registry import build_default_registry
from personal_news_agent.skills.hot_event_map import _sanitize_event_map_markdown


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


def test_tavily_provider_maps_search_results():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tvly-test"
        payload = json.loads(request.content)
        assert payload["query"] == "上海今天重要新闻"
        assert payload["search_depth"] == "basic"
        assert payload["max_results"] == 3
        assert payload["include_domains"] == ["shio.gov.cn"]
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://www.shio.gov.cn/news/1",
                        "title": "上海今日新闻",
                        "content": "上海发布最新消息。",
                        "score": 0.91,
                    }
                ]
            },
        )

    provider = TavilySearchProvider("tvly-test", transport=httpx.MockTransport(handler))
    results = asyncio.run(provider.search("上海今天重要新闻", ["shio.gov.cn"], 3))
    assert len(results) == 1
    assert results[0].source_id == "shio.gov.cn"
    assert results[0].title == "上海今日新闻"
    assert results[0].snippet == "上海发布最新消息。"


def test_external_provider_factory_selects_tavily():
    configured = Settings(external_search_provider="tavily", tavily_api_key="tvly-test")
    missing_key = Settings(external_search_provider="tavily", tavily_api_key=None)
    assert isinstance(external_provider_from_settings(configured), TavilySearchProvider)
    assert not external_provider_from_settings(missing_key).configured


def test_content_moderation_service_error_is_not_a_policy_block():
    service = TextModerationPlusService(access_key_id="ak", access_key_secret="sk")

    with pytest.raises(ContentModerationError) as exc_info:
        service._parse_result(
            {
                "Code": "Forbidden",
                "Message": "you haven’t activated the commodity:lvwang_cip_public_cn",
                "RequestId": "req-test",
            }
        )

    assert "lvwang_cip_public_cn" in str(exc_info.value)


def test_external_search_requires_explicit_permission(services):
    registry, store, _ = services
    provider = FakeExternalProvider()
    search = UnifiedSearchService(store, registry, external_provider=provider)
    asyncio.run(search.search("上海实时天气", ["tech"], None, None, 5))
    assert provider.calls == 0
    results = asyncio.run(search.search("上海实时天气", ["tech"], None, None, 5, include_remote=True))
    assert provider.calls == 1
    assert any(item.origin == "external" for item in results)


def test_external_semantic_results_are_not_dropped_by_exact_sentence_filter(services):
    registry, store, _ = services
    search = UnifiedSearchService(store, registry, external_provider=SemanticExternalProvider())
    query = "机车相关一些信息。提供一些开发商"
    assert _relevance_terms(query)[:2] == ["机车", "开发商"]
    results = asyncio.run(search.search_external(query, None, None, 5))
    assert [item.title for item in results] == ["全球主要摩托车制造商与品牌"]


def test_expansion_evidence_must_match_original_subject():
    query = "机车相关一些信息。提供一些开发商"
    unrelated = SearchResult(
        source_id="old-news",
        title="社会新闻汇总",
        url="https://example.com/old",
        summary="内容提到了房地产开发商和政策调整。",
        category="politics",
        origin="local",
    )
    related = SearchResult(
        source_id="motorcycle-news",
        title="当前机车品牌及主要制造商介绍",
        url="https://example.com/motorcycle",
        summary="介绍机车品牌、车型和制造企业。",
        category="auto",
        origin="external",
    )
    assert not search_result_matches_subject(query, unrelated)
    assert search_result_matches_subject(query, related)


def test_long_chinese_query_keeps_full_subject_ahead_of_short_prefixes():
    query = "背景事件对目标对象的影响 近期变化"
    terms = _relevance_terms(query)
    assert terms[0] == "背景事件对目标对象的影响"
    assert terms.index("背景") > terms.index("背景事件对目标对象的影响")


def test_planner_terms_filter_broad_background_results():
    unrelated = SearchResult(
        source_id="background",
        title="背景事件最新进展",
        url="https://example.com/background",
        summary="介绍背景事件本身的动态。",
        category="politics",
        origin="external",
    )
    related = SearchResult(
        source_id="subject",
        title="目标对象出现新的变化",
        url="https://example.com/subject",
        summary="分析目标对象受到的影响。",
        category="economy",
        origin="external",
    )
    required_terms = ["目标对象", "对象变化"]
    assert not search_result_matches_terms(required_terms, unrelated)
    assert search_result_matches_terms(required_terms, related)


def test_research_evidence_gate_rejects_same_city_but_unrelated_pages():
    plan = SearchQueryPlan(
        query="北京天津 防汛 应急响应",
        primary_subject="北京天津防汛应急响应",
        required_terms=["防汛", "暴雨", "应急响应"],
        keywords=["北京", "天津"],
        source="llm",
    )
    relevant = SearchResult(
        source_id="weather",
        title="中央气象台继续发布暴雨橙色预警",
        url="https://example.com/rain",
        summary="北京、天津启动防汛应急响应并关注强降雨。",
        category="politics",
        origin="external",
    )
    same_city_unrelated = SearchResult(
        source_id="tech",
        title="树科技已在北京天津布局合作项目",
        url="https://example.com/tree-tech",
        summary="企业在多地设立机构。",
        category="tech",
        origin="local",
    )
    unrelated = SearchResult(
        source_id="world",
        title="哥伦比亚发生地震",
        url="https://example.com/quake",
        summary="当地发布地震消息。",
        category="politics",
        origin="external",
    )

    assert _filter_research_results([same_city_unrelated, relevant, unrelated], plan) == [relevant]


def test_search_query_plan_is_generated_from_user_input(services):
    _, store, search = services
    llm = FakeSearchPlannerLLM()
    chat = NewsChatService(store, search, llm_client=llm)
    plan = asyncio.run(
        chat._plan_search_query(
            "背景事件对目标对象有什么影响？",
            None,
            "背景事件对目标对象 影响",
            TimeRange(days=14),
            True,
        )
    )
    assert plan.source == "llm"
    assert plan.query == "目标对象 影响 背景事件 近期变化"
    assert plan.primary_subject == "目标对象受到的影响"
    assert plan.required_terms == ["目标对象", "对象变化"]
    assert llm.calls == 1


def test_search_query_plan_falls_back_without_web_permission(services):
    _, store, search = services
    llm = FakeSearchPlannerLLM()
    chat = NewsChatService(store, search, llm_client=llm)
    plan = asyncio.run(chat._plan_search_query("原始问题", None, "完整原始查询", None, False))
    assert plan.query == "完整原始查询"
    assert plan.source == "fallback"
    assert llm.calls == 0


def test_contextual_followup_is_rewritten_with_local_agent_memory(services):
    _, store, _ = services
    store.save_turn(
        "ctx_conv",
        "现在有什么烹饪机器人牌子？",
        "代表品牌包括苏泊尔、添可、九阳、ChefRobot、长膳和智谷天厨。",
        [],
        {"type": "topic", "text": "ai烹饪机器人"},
        user_id="default",
        topic="ai烹饪机器人",
        category_scope=["tech"],
    )
    search = CaptureQuerySearchService()
    local_agent = FakeContextRewriteLocalAgent("苏泊尔 添可 九阳 ChefRobot 长膳 智谷天厨 公司特色")
    chat = NewsChatService(store, search, local_agent=local_agent, llm_client=FakeDisabledLLM())

    asyncio.run(
        chat.chat(
            "ctx_conv",
            "上面那些公司都有什么特色",
            topic="ai烹饪机器人",
            category_scope=["tech"],
            user_id="default",
        )
    )

    assert local_agent.calls == 1
    assert "苏泊尔" in local_agent.last_payload.project_context["recent_memory"]
    assert "苏泊尔" in search.queries[0]
    assert "ChefRobot" in search.queries[0]
    assert "公司特色" in search.queries[0]


def test_related_search_uses_local_agent_queries_and_saves_turn(services):
    _, store, _ = services
    search = FakeRelatedSearchService()
    local_agent = FakeRelatedLocalAgent()
    chat = NewsChatService(store, search, local_agent=local_agent)
    response = asyncio.run(
        chat.related_search(
            "related_conv",
            "AI Agent",
            category_scope=["tech"],
            user_id="default",
            max_queries=3,
        )
    )
    assert response.context_relation == "related_search"
    assert [item["query"] for item in response.expanded_queries] == ["AI Agent 最新进展", "AI Agent 产业影响", "AI Agent 背景 脉络"]
    assert response.mind_map
    assert response.mind_map["type"] == "related_research_path_v2"
    assert response.mind_map["topic"] == "AI Agent"
    assert [step["kind"] for step in response.mind_map["steps"][:2]] == ["context", "resolution"]
    assert response.mind_map["steps"][-1]["kind"] == "conclusion"
    assert [branch["title"] for branch in response.mind_map["branches"]] == ["AI Agent 最新进展", "AI Agent 产业影响", "AI Agent 背景 脉络"]
    assert response.mind_map["branches"][0]["relation_label"] == "最新进展"
    assert response.mind_map["branches"][0]["edge_reason"] == "查看近期变化"
    assert response.mind_map["branches"][0]["points"][0]["title"] == "AI Agent 最新进展 报道"
    assert response.mind_map["branches"][0]["points"][0]["connection_reason"] == "检索命中「最新进展」方向"
    assert "相关思维导图" in response.answer
    assert search.queries == ["AI Agent 最新进展", "AI Agent 产业影响", "AI Agent 背景 脉络"]
    assert response.recommendations
    turns = store.list_turns("related_conv", "default")
    assert turns[-1]["response"]["context_relation"] == "related_search"


def test_related_base_query_prefers_latest_hot_event_title():
    raw = (
        "现在什么水果利率最高 围绕热点事件“农学热！超八成受访高考生和家长感觉大家对涉农专业看法有改观”展开，"
        "发生了什么、为什么重要、后续看什么。 围绕热点事件“强化问题导向推进作风建设相关热点：强化问题导向推进作风建设--党建-中国共产党新闻网”展开，"
        "发生了什么、为什么重要、后续看什么。"
    )
    assert _related_base_query(raw, raw) == "强化问题导向推进作风建设"


def test_related_context_query_keeps_previous_topic_as_anchor():
    assert _related_context_query("dota2 TI 上海", "ame") == "dota2 TI 上海 中的 ame"
    assert _related_context_query("dota2 TI 上海", "dota2") == "dota2 TI 上海"


def test_related_result_filter_rejects_latin_substring_coincidences():
    unrelated = SearchResult(
        source_id="gamersky",
        title="沈腾新片 _ GamerSky.com",
        url="https://example.com/movie",
        summary="上海暑期档电影资讯",
        category="entertainment",
        origin="local",
    )
    related = SearchResult(
        source_id="esports",
        title="Dota 2 选手 Ame 备战 TI",
        url="https://example.com/ame-ti",
        summary="Ame 与中国战队参加上海赛事的动态。",
        category="game",
        origin="local",
    )

    filtered = _filter_related_runtime_results([unrelated, related], "dota2 TI 上海", "ame")

    assert filtered == [related]


def test_related_search_uses_cc_context_and_web_for_ambiguous_player_name(services):
    _, store, search = services
    store.save_turn(
        "dota_related_conv",
        "dota2 TI 上海",
        "上一轮讨论了 TI 上海赛事。",
        [],
        {"type": "topic", "text": "dota2 TI 上海"},
        user_id="related_user",
        topic="dota2 TI 上海",
        category_scope=["game"],
    )
    runtime = FakeRelatedCCRuntime()
    chat = NewsChatService(store, search, cc_runtime=runtime)

    response = asyncio.run(
        chat.related_search(
            "dota_related_conv",
            "ame",
            user_id="related_user",
            allow_web_search=True,
        )
    )

    assert runtime.kwargs["query"] == "dota2 TI 上海 中的 ame"
    assert runtime.kwargs["topic"] == "dota2 TI 上海"
    assert "上一轮讨论了 TI 上海赛事" in runtime.kwargs["history"]
    assert runtime.kwargs["skill_names"] == [NEWS_RELATED_EXPLORATION_SKILL_NAME]
    assert runtime.kwargs["allow_web_search"] is True
    assert runtime.kwargs["max_turns"] == 10
    assert runtime.kwargs["timeout_seconds"] == 180.0
    assert runtime.kwargs["builtin_web_search_limit"] == 3
    assert response.context_relation == "related_search_cc_runtime"
    assert response.topic == "dota2 TI 上海"
    assert response.focus_object.text == "dota2 TI 上海 中的 ame"
    assert [item.url for item in response.recommendations] == ["https://example.com/ame-ti"]
    assert "关联较弱" not in response.answer
    assert response.mind_map["type"] == "related_research_path_v2"
    assert response.mind_map["active_topic"] == "dota2 TI 上海"
    assert response.mind_map["requested_focus"] == "ame"
    assert response.mind_map["steps"][0]["label"] == "语境锚点"
    assert any(step["kind"] == "web_search" for step in response.mind_map["steps"])


def test_related_search_uses_clean_hot_event_topic(services):
    _, store, _ = services
    search = FakeRelatedSearchService()
    chat = NewsChatService(store, search, local_agent=FakeRelatedLocalAgent())
    raw = (
        "现在什么水果利率最高 围绕热点事件“农学热！超八成受访高考生和家长感觉大家对涉农专业看法有改观”展开，"
        "发生了什么、为什么重要、后续看什么。 围绕热点事件“强化问题导向推进作风建设相关热点：强化问题导向推进作风建设--党建-中国共产党新闻网”展开，"
        "发生了什么、为什么重要、后续看什么。"
    )
    response = asyncio.run(
        chat.related_search(
            "related_dirty_conv",
            raw,
            category_scope=["economy"],
            user_id="default",
            max_queries=3,
        )
    )
    assert response.mind_map["topic"] == "强化问题导向推进作风建设"
    assert response.topic == "强化问题导向推进作风建设"
    assert all("农学热" not in query for query in search.queries)
    assert all("水果" not in query for query in search.queries)


def test_chat_uses_explicit_hot_event_as_current_focus(services):
    _, store, _ = services
    search = RecordingSearchService()
    chat = NewsChatService(store, search, llm_client=FakeDisabledLLM())
    message = (
        "追踪「人民日报理论版--理论--人民网」是否出现后续回应或新进展。"
        "围绕热点事件“中新网相关热点：徽州文化艺术展亮相上海市历史博物馆-中新网”展开，"
        "告诉我发生了什么、为什么重要、后续看什么。"
    )

    response = asyncio.run(
        chat.chat(
            "unrelated_followup_conv",
            message,
            topic="人民日报理论版--理论--人民网",
            category_scope=["politics"],
            use_llm=True,
            user_id="default",
        )
    )

    assert response.topic == "人民日报理论版--理论--人民网"
    assert response.focus_object is not None
    assert response.focus_object.type == "topic"
    assert response.focus_object.text == "徽州文化艺术展亮相上海市历史博物馆"
    assert search.calls[0]["query"] == "徽州文化艺术展亮相上海市历史博物馆"
    assert search.calls[0]["category_scope"] is None
    assert response.attention_suggestion is None
    assert "手动新增关注" not in response.answer
    assert all(item.get("stage") != "主题关系" for item in response.research_trace)


def test_chat_uses_explicit_article_title_over_stale_topic(services):
    _, store, _ = services
    search = RecordingSearchService()
    chat = NewsChatService(store, search, llm_client=FakeDisabledLLM())

    response = asyncio.run(
        chat.chat(
            "stale_topic_article_conv",
            "基于资讯“人气动画新篇章定档”继续深挖，给我结论、证据和后续观察点。",
            topic="inappropriate_oral",
            category_scope=["inappropriate_oral"],
            use_llm=True,
            user_id="default",
        )
    )

    assert response.topic == "inappropriate_oral"
    assert response.focus_object is not None
    assert response.focus_object.text == "人气动画新篇章定档"
    assert search.calls[0]["query"] == "人气动画新篇章定档"
    assert search.calls[0]["category_scope"] == ["anime"]


def test_related_answer_lists_all_merged_evidence():
    evidence = [
        {
            "index": index,
            "title": f"证据标题 {index}",
            "source_id": "example.com",
            "published_at": None,
            "summary": "摘要",
            "url": f"https://example.com/{index}",
        }
        for index in range(1, 13)
    ]
    answer = _related_search_answer("测试主题", [], evidence, "fallback")
    assert "合并证据：12 条" in answer
    assert "证据 12｜证据标题 12" in answer


def test_chat_source_ingestion_requires_explicit_permission(services):
    _, store, search = services
    ingestion = FakeNativeIngestion()
    chat = NewsChatService(store, search, llm_client=FakeDisabledLLM(), native_ingestion=ingestion)
    disabled = asyncio.run(chat._research_chat("offline", "上海天气", allow_web_search=False))
    assert ingestion.calls == 0
    assert any(item["stage"] == "新闻源更新" and item["status"] == "skipped" for item in disabled.research_trace)

    asyncio.run(chat._research_chat("online", "上海天气", allow_web_search=True))
    assert ingestion.calls == 1


def test_research_empty_evidence_does_not_ask_llm_to_invent_answer(services):
    _, store, _ = services
    search = EmptySearchService()
    llm = FakeAnswerLLM()
    chat = NewsChatService(store, search, llm_client=llm)

    response = asyncio.run(chat._research_chat("empty_research_conv", "网上购物的发生过的事故都有哪些", allow_web_search=False))

    assert llm.calls == 0
    assert response.context_relation == "research_pipeline_empty"
    assert "当前没有召回到可引用证据" in response.answer
    assert "常见类型" not in response.answer


def test_research_always_uses_external_search_when_web_enabled(services):
    _, store, _ = services
    search = EnoughLocalWithExternalSearchService()
    chat = NewsChatService(store, search, llm_client=FakeDisabledLLM())

    response = asyncio.run(chat._research_chat("external_required_conv", "证监会在2026年做了什么事", allow_web_search=True))

    assert search.external_calls
    assert any(item["stage"] == "外部搜索工具" and item["status"] == "completed" for item in response.research_trace)


def test_research_does_not_use_external_search_when_web_disabled(services):
    _, store, _ = services
    search = EnoughLocalWithExternalSearchService()
    chat = NewsChatService(store, search, llm_client=FakeDisabledLLM())

    response = asyncio.run(chat._research_chat("external_disabled_conv", "证监会在2026年做了什么事", allow_web_search=False))

    assert search.external_calls == []
    assert not any(item["stage"] == "外部搜索工具" for item in response.research_trace)


def test_cc_runtime_search_tools_use_existing_local_and_web_services(services):
    _, store, _ = services
    search = RecordingSearchService()
    emitted = []

    async def on_trace(item):
        emitted.append(item)

    context = RuntimeSearchContext(store, search, ["tech"], None, allow_web_search=True, on_trace=on_trace)

    local_payload = asyncio.run(context.local_news_search({"query": "AI Agent", "limit": 4}))
    web_payload = asyncio.run(context.web_search({"query": "AI Agent 最新进展", "limit": 3}))

    assert local_payload["structuredContent"]["ok"] is True
    assert web_payload["structuredContent"]["ok"] is True
    assert search.calls[0]["include_remote"] is False
    assert search.external_calls
    assert [item.origin for item in context.results] == ["local", "external"]
    assert [item["origin"] for item in context.queries] == ["local", "web"]
    assert context.queries[0]["result_urls"] == ["https://example.com/unrelated"]
    assert context.queries[1]["result_urls"] == ["https://web.example/factcheck"]
    assert [item["stage"] for item in emitted] == ["本地新闻引擎", "外部搜索工具"]


def test_cc_runtime_web_tool_refuses_unapproved_web_search(services):
    _, store, _ = services
    search = RecordingSearchService()
    context = RuntimeSearchContext(store, search, ["tech"], None, allow_web_search=False)

    payload = asyncio.run(context.web_search({"query": "越权联网", "limit": 3}))

    assert payload["isError"] is True
    assert payload["structuredContent"]["error_type"] == "PermissionError"
    assert search.external_calls == []


def test_cc_runtime_options_expose_only_read_only_news_tools(services, tmp_path):
    _, store, search = services
    runtime = CCRuntimeOrchestrator(
        store,
        search,
        Settings(
            cc_runtime_enabled=True,
            cc_runtime_auth_token="test-only",
            cc_runtime_base_url="https://dashscope.aliyuncs.com/apps/anthropic",
            cc_runtime_model="deepseek-v4-flash",
            cc_runtime_config_dir=tmp_path / "cc-runtime",
        ),
    )
    context = RuntimeSearchContext(store, search, ["tech"], None, allow_web_search=False)

    options = runtime.build_options(context)

    assert options.tools == []
    assert options.allowed_tools == [LOCAL_TOOL_NAME]
    assert options.setting_sources == ["project"]
    assert options.skills == []
    assert WEB_TOOL_NAME not in options.allowed_tools
    assert "Bash" in options.disallowed_tools
    assert "Write" in options.disallowed_tools
    assert options.permission_mode == "dontAsk"
    assert options.strict_mcp_config is True
    assert "test-only" not in repr(runtime.settings)

    web_context = RuntimeSearchContext(store, RecordingSearchService(), ["tech"], None, allow_web_search=True)
    web_options = CCRuntimeOrchestrator(
        store,
        web_context.search_service,
        runtime.settings,
    ).build_options(web_context)
    assert web_options.tools == ["WebSearch"]
    assert web_options.allowed_tools == [LOCAL_TOOL_NAME, WEB_TOOL_NAME, "WebSearch"]
    assert "必须在生成最终答案前调用 CC 自带的 WebSearch" in web_options.system_prompt
    assert "News Agent（元融个人资讯助手）" in web_options.system_prompt
    assert "不得提及 Claude" in web_options.system_prompt
    assert "本地新闻引擎" in web_options.system_prompt
    assert "外部搜索工具" in web_options.system_prompt

    skill_options = runtime.build_options(context, ["news-fact-check"])
    assert skill_options.tools == ["Skill"]
    assert skill_options.skills == ["news-fact-check"]
    assert skill_options.allowed_tools == ["Skill", LOCAL_TOOL_NAME]
    assert "Skill" not in skill_options.disallowed_tools

    related_skill_options = runtime.build_options(context, [NEWS_RELATED_EXPLORATION_SKILL_NAME])
    assert related_skill_options.skills == [NEWS_RELATED_EXPLORATION_SKILL_NAME]

    limited_web_options = runtime.build_options(web_context, builtin_web_search_limit=3)
    assert limited_web_options.hooks["PreToolUse"][0].matcher == "WebSearch"
    web_hook = limited_web_options.hooks["PreToolUse"][0].hooks[0]
    hook_results = [
        asyncio.run(web_hook({"tool_name": "WebSearch"}, None, {}))
        for _ in range(4)
    ]
    assert [item["hookSpecificOutput"]["permissionDecision"] for item in hook_results] == [
        "allow",
        "allow",
        "allow",
        "deny",
    ]

    builtin_web_context = RuntimeSearchContext(store, search, ["tech"], None, allow_web_search=True)
    builtin_web_options = runtime.build_options(builtin_web_context, ["news-fact-check"])
    assert builtin_web_options.tools == ["Skill", "WebSearch"]
    assert builtin_web_options.allowed_tools == ["Skill", LOCAL_TOOL_NAME, "WebSearch"]
    assert "WebSearch" not in builtin_web_options.disallowed_tools

    with pytest.raises(ValueError, match="Unsupported project skill"):
        runtime.build_options(context, ["arbitrary-skill"])

    no_local_context = RuntimeSearchContext(
        store,
        search,
        ["tech"],
        None,
        allow_web_search=False,
        allow_local_search=False,
    )
    no_local_options = runtime.build_options(no_local_context)
    assert no_local_options.allowed_tools == []


def test_cc_runtime_product_identity_hides_internal_runtime_names():
    prompt = _system_prompt(web_enabled=True)

    assert "只介绍 News Agent" in prompt
    assert "实际模型供应商" in prompt
    assert "面向用户只使用“本地新闻引擎”和“外部搜索工具”" in prompt
    assert "不得把公开抓取或检索到的新闻网站称为合作方" in prompt


def test_cc_runtime_run_normalizes_sdk_result_without_changing_business_schema(services, tmp_path):
    _, store, search = services
    runtime = CCRuntimeOrchestrator(
        store,
        search,
        Settings(
            cc_runtime_enabled=True,
            cc_runtime_auth_token="test-only",
            cc_runtime_base_url="https://dashscope.aliyuncs.com/apps/anthropic",
            cc_runtime_model="deepseek-v4-flash",
            cc_runtime_config_dir=tmp_path / "cc-runtime-run",
        ),
        client_factory=FakeClaudeSDKClient,
    )

    result = asyncio.run(
        runtime.run(
            message="总结 AI Agent 变化",
            query="AI Agent 变化",
            topic="AI Agent",
            category_scope=["tech"],
            time_range=None,
            history="无",
            allow_web_search=False,
        )
    )

    assert result.answer == "## SDK 最终回答"
    assert result.provider_metadata["runtime"] == "claude-agent-sdk"
    assert result.provider_metadata["num_turns"] == 2


def test_cc_runtime_retries_until_builtin_web_search_runs(services, tmp_path):
    _, store, search = services
    runtime = CCRuntimeOrchestrator(
        store,
        search,
        Settings(
            cc_runtime_enabled=True,
            cc_runtime_auth_token="test-only",
            cc_runtime_base_url="https://dashscope.aliyuncs.com/apps/anthropic",
            cc_runtime_model="deepseek-v4-flash",
            cc_runtime_config_dir=tmp_path / "cc-runtime-required-web",
        ),
        client_factory=FakeWebRequiredClaudeSDKClient,
    )

    result = asyncio.run(
        runtime.run(
            message="总结 AI Agent 变化",
            query="AI Agent 变化",
            topic="AI Agent",
            category_scope=["tech"],
            time_range=None,
            history="无",
            allow_web_search=True,
        )
    )

    assert result.answer == "## 已核对外部信息的最终回答"
    assert result.provider_metadata["builtin_web_calls"] == 1
    assert any("首轮未执行实时检索" in item["message"] for item in result.trace)
    assert any(item["stage"] == "外部搜索工具" and item["status"] == "completed" for item in result.trace)


def test_research_chat_uses_cc_runtime_without_changing_response_contract(services):
    _, store, _ = services
    search = RecordingSearchService()
    runtime = FakeCCRuntime()
    chat = NewsChatService(store, search, llm_client=FakeDisabledLLM(), cc_runtime=runtime)

    response = asyncio.run(
        chat.chat(
            "cc_runtime_conv",
            "AI Agent 最近有什么变化",
            category_scope=["tech"],
            use_llm=True,
            user_id="default",
            allow_web_search=True,
            model_key="qwen3.6",
        )
    )

    assert runtime.calls == 1
    assert runtime.logical_model_key == "qwen3.6"
    assert runtime.skill_names == [NEWS_CONVERSATION_RESEARCH_SKILL_NAME]
    assert response.context_relation == "research_pipeline_cc_runtime"
    assert response.answer == "## Runtime 汇总\n\n证据支持这项变化。"
    assert response.recommendations
    assert response.evidence
    assert response.expanded_queries[0]["origin"] == "local"
    assert any(item["stage"] == "Agent 主控" for item in response.research_trace)


def test_research_chat_stream_emits_public_harness_execution_steps(services):
    _, store, _ = services
    search = RecordingSearchService()
    chat = NewsChatService(store, search, llm_client=FakeDisabledLLM(), cc_runtime=FakeCCRuntime())

    async def collect_events():
        return [
            event
            async for event in chat.chat_events(
                "cc_runtime_stream_conv",
                "AI Agent 最近有什么变化",
                category_scope=["tech"],
                use_llm=True,
                user_id="default",
                allow_web_search=True,
            )
        ]

    events = asyncio.run(collect_events())
    trace_items = [event["item"] for event in events if event["type"] == "trace"]

    assert events[0]["type"] == "start"
    assert any(item["stage"] == "分析计划" for item in trace_items)
    assert any(item["stage"] == "Agent 主控" and item["status"] == "running" for item in trace_items)
    assert any(item["stage"] == "证据合并" for item in trace_items)
    assert events[-1]["type"] == "final"
    final_trace = events[-1]["response"]["research_trace"]
    terminal_stages = {item["stage"] for item in final_trace if item["status"] != "running"}
    assert not any(item["stage"] in terminal_stages and item["status"] == "running" for item in final_trace)


def test_general_chat_uses_cc_without_business_skill_or_local_search(services):
    _, store, _ = services
    runtime = FakeCCRuntime()
    chat = NewsChatService(store, RecordingSearchService(), llm_client=FakeDisabledLLM(), cc_runtime=runtime)

    response = asyncio.run(
        chat.chat(
            "general_cc_conv",
            "你好",
            use_llm=True,
            user_id="general_cc_user",
            allow_web_search=True,
        )
    )

    assert response.context_relation == "general_conversation_cc_runtime"
    assert runtime.skill_names == []
    assert runtime.last_kwargs["allow_local_search"] is False
    assert any("不加载业务 Skill" in item["message"] for item in response.research_trace)


def test_general_knowledge_uses_no_skill_but_current_news_uses_research_skill(services):
    _, store, _ = services
    general_runtime = FakeCCRuntime()
    general_chat = NewsChatService(store, RecordingSearchService(), llm_client=FakeDisabledLLM(), cc_runtime=general_runtime)
    general = asyncio.run(
        general_chat.chat(
            "general_knowledge_conv",
            "什么是向量数据库？",
            use_llm=True,
            user_id="general_knowledge_user",
            allow_web_search=True,
        )
    )
    assert general.context_relation == "general_conversation_cc_runtime"
    assert general_runtime.skill_names == []

    news_runtime = FakeCCRuntime()
    news_chat = NewsChatService(store, RecordingSearchService(), llm_client=FakeDisabledLLM(), cc_runtime=news_runtime)
    current_news = asyncio.run(
        news_chat.chat(
            "current_news_conv",
            "AI Agent 今天有什么最新进展？",
            use_llm=True,
            user_id="current_news_user",
            allow_web_search=True,
        )
    )
    assert current_news.context_relation == "research_pipeline_cc_runtime"
    assert news_runtime.skill_names == [NEWS_CONVERSATION_RESEARCH_SKILL_NAME]


def test_live_service_queries_pass_the_original_message_to_general_cc(services):
    _, store, _ = services
    questions = [
        "今天上海天气怎么样，出门需要带伞吗？",
        "上海虹桥火车站到西湖景区怎么走？",
        "明天北京到上海有哪些高铁？",
        "CA1832 明天是否延误？",
    ]
    for index, question in enumerate(questions):
        runtime = FakeCCRuntime()
        chat = NewsChatService(store, RecordingSearchService(), llm_client=FakeDisabledLLM(), cc_runtime=runtime)
        response = asyncio.run(
            chat.chat(
                f"general_live_info_{index}",
                question,
                use_llm=True,
                user_id=f"general_live_info_user_{index}",
                allow_web_search=True,
            )
        )

        assert response.context_relation == "general_conversation_cc_runtime"
        assert runtime.skill_names == []
        assert runtime.last_kwargs["allow_web_search"] is True
        assert runtime.last_kwargs["allow_local_search"] is False
        assert runtime.last_kwargs["allow_everyday_tools"] is True
        assert runtime.last_kwargs["require_builtin_web_search"] is False
        assert runtime.last_kwargs["message"] == question
        assert response.answer == "## Runtime 汇总\n\n证据支持这项变化。"


def test_current_news_wording_is_not_misrouted_as_general_knowledge(services):
    _, store, _ = services
    runtime = FakeCCRuntime()
    chat = NewsChatService(store, RecordingSearchService(), llm_client=FakeDisabledLLM(), cc_runtime=runtime)

    response = asyncio.run(
        chat.chat(
            "current_news_how_conv",
            "如何看待今天最新的电影票房新闻？",
            use_llm=True,
            user_id="current_news_how_user",
            allow_web_search=True,
        )
    )

    assert response.context_relation == "research_pipeline_cc_runtime"
    assert runtime.skill_names == [NEWS_CONVERSATION_RESEARCH_SKILL_NAME]


def test_research_chat_falls_back_when_cc_runtime_fails(services):
    _, store, _ = services
    search = RecordingSearchService()
    chat = NewsChatService(store, search, llm_client=FakeDisabledLLM(), cc_runtime=FakeFailingCCRuntime())

    response = asyncio.run(
        chat._research_chat(
            "cc_runtime_fallback_conv",
            "AI Agent 最近有什么变化",
            category_scope=["tech"],
            allow_web_search=False,
        )
    )

    assert response.context_relation != "research_pipeline_cc_runtime"
    assert any(item["stage"] == "Agent 主控" and item["status"] == "fallback" for item in response.research_trace)


def test_web_regular_chat_does_not_auto_create_topic_card():
    source = Path("personal_news_agent/static/web.js").read_text()
    regular_branch = source.split("if (!command) {", 1)[1].split("appendLocalTurn(\"user\", message);", 1)[0]

    assert "sendChat(message)" in regular_branch
    assert "createTopicFromFirstMessage" not in regular_branch
    assert "if (!pendingTopicFromNextMessage && options.force !== true) return null;" in source


def test_web_and_mobile_load_dynamic_recommended_topics():
    web_source = Path("personal_news_agent/static/web.js").read_text()
    mobile_source = Path("personal_news_agent/static/mobile.js").read_text()

    assert "/api/topics/recommended?" in web_source
    assert "recommended.items || []" in web_source
    assert "item.hot_score" in web_source
    assert "/api/topics/recommended?" in mobile_source
    assert "recommended.items || []" in mobile_source


def test_web_chat_response_resets_related_rail_per_query():
    source = Path("personal_news_agent/static/web.js").read_text()
    side_effects = source.split("function handleWebChatResponseSideEffects(response) {", 1)[1].split("function responseScopedArticles", 1)[0]

    assert "responseScopedArticles(response)" in side_effects
    assert "renderResponseScopedFeed(articles)" in side_effects
    assert "response?.event_line?.items || []" in side_effects
    assert "本轮暂无相关资讯" in source
    assert "topicOverride || consoleState.topic" in source


def test_web_filters_system_labels_from_related_entities():
    source = Path("personal_news_agent/static/web.js").read_text()

    assert "!isSystemEntityLabel(node.label)" in source
    assert "暂无足够入库资料" in source
    assert "后续将通过源搜索" in source
    assert "抓取和大模型抽取补全" in source


def test_web_markdown_tables_render_as_tables():
    source = Path("personal_news_agent/static/shared.js").read_text()
    styles = Path("personal_news_agent/static/styles.css").read_text()

    assert "isMarkdownTableStart(lines, index)" in source
    assert "<table><thead><tr>" in source
    assert "markdown-table-wrap" in source
    assert ".assistant-markdown table" in styles


def test_web_markdown_long_factcheck_lines_wrap_inside_bubble():
    styles = Path("personal_news_agent/static/styles.css").read_text()
    markdown_rule = styles.split(".assistant-markdown {", 1)[1].split("}", 1)[0]
    text_rule = styles.split(".assistant-markdown p,", 1)[1].split("}", 1)[0]
    link_rule = styles.split(".assistant-markdown a {", 1)[1].split("}", 1)[0]

    assert "max-width: 100%" in markdown_rule
    assert "overflow-wrap: anywhere" in markdown_rule
    assert "word-break: break-word" in text_rule
    assert "overflow-wrap: anywhere" in link_rule


def test_check_skill_is_not_registered_or_shown_in_command_menu():
    registry = build_default_registry()
    commands = [item.command for item in registry.list_skills()]
    shared_source = Path("personal_news_agent/static/shared.js").read_text()
    web_source = Path("personal_news_agent/static/web.js").read_text()
    mobile_source = Path("personal_news_agent/static/mobile.js").read_text()
    home_source = Path("personal_news_agent/static/home.js").read_text()
    home_html = Path("personal_news_agent/static/home.html").read_text()
    mobile_html = Path("personal_news_agent/static/mobile.html").read_text()

    assert "/check" not in commands
    assert "/map" in commands
    assert "/related" in commands
    assert 'name: "check"' not in shared_source
    assert "可执行：/check" not in web_source
    assert "可执行：/check" not in mobile_source
    assert "20260812-chat-workspace-1" in home_source
    assert "styles.css?v=20260812-chat-workspace-1" in home_html
    assert "shared.js?v=20260810-phone-controls-2" in mobile_html


def test_related_command_uses_streaming_chat_skill_on_web_and_mobile():
    web_source = Path("personal_news_agent/static/web.js").read_text()
    mobile_source = Path("personal_news_agent/static/mobile.js").read_text()

    web_related = web_source.split("async function runRelatedSearchIntoTurn", 1)[1].split("async function", 1)[0]
    mobile_related = mobile_source.split("async function runMobileRelatedSearchIntoTurn", 1)[1].split("async function", 1)[0]
    assert 'sendChatIntoTurn(relatedCommand, assistantNode)' in web_related
    assert 'sendChatIntoTurn(relatedCommand, assistantNode)' in mobile_related
    assert 'request("/api/news/related"' not in web_related
    assert 'request("/api/news/related"' not in mobile_related


def test_time_filter_keeps_current_external_results_without_published_date():
    old_local = SearchResult(
        source_id="local",
        title="上海旧闻",
        url="https://example.com/old",
        summary="上海天气",
        category="tech",
        published_at=datetime.now(timezone.utc) - timedelta(days=10),
        origin="local",
    )
    live_external = SearchResult(
        source_id="shio.gov.cn",
        title="上海实时消息",
        url="https://www.shio.gov.cn/live",
        summary="上海今天的重要消息",
        category="tech",
        origin="external",
    )
    filtered = _filter_by_time([old_local, live_external], TimeRange(days=1))
    ranked = _rank_for_chat(filtered, "上海今天有什么重要新闻")
    assert [item.url for item in ranked] == [live_external.url]


def test_chat_understanding_overrides_stale_sports_scope_for_game_topic():
    assert categories_for_message("我想知道游戏资讯的最近消息", "当前机车品牌", ["sports"]) == ["game"]
    assert query_from_message("我想知道游戏资讯的最近消息", "当前机车品牌") == "游戏资讯 消息"


def test_chat_understanding_preserves_auto_brand_terms():
    assert query_from_message("追踪汽车之家是否出现后续回应") == "追踪汽车之家是否出现后续回应"
    assert "新能源汽车" in query_from_message("近一个月新能源汽车价格战有哪些值得关注的新变化？")


def test_chat_understanding_keeps_topic_for_contextual_followup():
    query = query_from_message("还有别的什么机车比赛？我也想知道他们这方面的消息", "当前机车品牌")
    assert query.startswith("当前机车品牌")
    assert categories_for_message("还有别的什么机车比赛？", "当前机车品牌", ["sports"]) == ["sports"]


def test_chat_understanding_preserves_comparison_intent_with_current_topic():
    message = "给我一些别的机车公司的信息，这些公司要和当前机车品牌很像"
    query = query_from_message(message, "当前机车品牌")
    assert query != "当前机车品牌"
    assert "机车公司" in query
    assert "当前机车品牌" in query
    assert "很像" in query
    assert categories_for_message(message, "当前机车品牌", ["sports"]) == ["auto"]


def test_conversation_context_uses_last_saved_topic(services):
    _, store, search = services
    store.save_turn("context_memory", "介绍主题甲", "主题甲回答", [], None, user_id="user_context", topic="主题甲")
    chat = NewsChatService(store, search)
    topic, categories = chat._resolve_conversation_context(
        "context_memory",
        "继续比较刚刚问的内容",
        "默认主题",
        None,
        "user_context",
    )
    assert topic == "主题甲"
    assert categories is None


def test_conversation_context_uses_recent_topic_across_conversations(services):
    _, store, search = services
    store.save_turn(
        "previous_context_memory",
        "介绍主题甲",
        "主题甲回答",
        [],
        None,
        user_id="cross_context_user",
        topic="主题甲",
        category_scope=["auto"],
    )
    chat = NewsChatService(store, search)

    topic, categories = chat._resolve_conversation_context(
        "new_context_memory",
        "继续说说刚才那个",
        None,
        None,
        "cross_context_user",
    )

    assert topic == "主题甲"
    assert categories == ["auto"]
    memory = chat._conversation_memory("new_context_memory", "cross_context_user")
    assert any(item["conversation_id"] == "previous_context_memory" for item in memory)


def test_chat_does_not_auto_create_attention_topic_for_normal_question(services):
    _, store, search = services
    reports = ReportGenerationService(store, search)
    tasks = ScheduledTaskService(store, reports)
    topic_agent = TopicAgentService(store, tasks)
    chat = NewsChatService(store, search, topic_agent=topic_agent)

    response = asyncio.run(
        chat.chat(
            "normal_question_topic",
            "张雪机车有什么值得关注的新变化？",
            topic="张雪机车",
            category_scope=["sports"],
            user_id="topic_guard_user",
        )
    )

    assert response.context_relation != "topic_agent_created"
    topics = [item for item in store.list_topics("topic_guard_user") if item["topic_type"] == "user"]
    assert topics == []


def test_chat_quoted_tracking_subject_does_not_create_attention_card(services):
    _, store, search = services
    chat = NewsChatService(store, search)

    response = asyncio.run(
        chat.chat(
            "quoted_tracking_topic",
            "追踪「汽车之家_看车 买车 用车 换车,省时省心省钱!」是否出现后续回应或新进展。",
            category_scope=["auto"],
            user_id="quoted_topic_user",
        )
    )

    topics = [item for item in store.list_topics("quoted_topic_user") if item["topic_type"] == "user"]
    assert response.focus_object is not None
    assert response.focus_object.type == "topic"
    assert topics == []


def test_chat_new_question_answers_without_changing_topic_or_creating_card(services):
    _, store, _ = services
    search = RecordingSearchService()
    reports = ReportGenerationService(store, search)
    tasks = ScheduledTaskService(store, reports)
    topic_agent = TopicAgentService(store, tasks)
    chat = NewsChatService(store, search, topic_agent=topic_agent)

    response = asyncio.run(
        chat.chat(
            "off_topic_notice_conv",
            "现在的ai公司们的发展前景是怎么样的",
            topic="用户与游戏公司起冲突的案例",
            category_scope=["game"],
            user_id="off_topic_user",
        )
    )

    assert response.context_relation == "topic_grounded"
    assert response.topic == "用户与游戏公司起冲突的案例"
    assert response.focus_object is not None
    assert response.focus_object.text == "用户与游戏公司起冲突的案例"
    assert "当前关注主题关联较弱" in response.answer
    assert search.calls
    assert "ai公司" in search.calls[0]["query"]
    assert "发展前景" in search.calls[0]["query"]
    topics = [item for item in store.list_topics("off_topic_user") if item["topic_type"] == "user"]
    assert topics == []
    turn = store.last_turn("off_topic_notice_conv", user_id="off_topic_user")
    assert turn["topic"] == "用户与游戏公司起冲突的案例"


def test_explicit_hot_event_compares_against_previous_turn_when_topic_matches_message_focus(services):
    _, store, _ = services
    search = RecordingSearchService()
    chat = NewsChatService(store, search)
    first_focus = "美国马里兰州青少年探访黄山 沉浸式感受中国山水魅力"
    second_focus = "军工装备板块再度走强"

    first = asyncio.run(
        chat.chat(
            "hot_event_drift_conv",
            f"围绕热点事件“中新网相关热点：{first_focus}-中新网”展开，告诉我发生了什么、为什么重要、后续看什么。",
            topic=first_focus,
            user_id="hot_event_drift_user",
        )
    )
    second = asyncio.run(
        chat.chat(
            "hot_event_drift_conv",
            f"围绕热点事件“{second_focus}”展开，告诉我发生了什么、为什么重要、后续看什么。",
            topic=second_focus,
            user_id="hot_event_drift_user",
        )
    )

    assert "当前关注主题关联较弱" not in first.answer
    assert "当前关注主题关联较弱" in second.answer
    assert second.topic == first_focus
    assert second.focus_object is not None
    assert second.focus_object.text == second_focus


def test_placeholder_topic_is_replaced_by_first_valid_query(services):
    _, store, _ = services
    search = RecordingSearchService()
    chat = NewsChatService(store, search)

    response = asyncio.run(
        chat.chat(
            "placeholder_topic_conv",
            "围绕热点事件“军工装备板块再度走强”展开，告诉我发生了什么、为什么重要、后续看什么。",
            topic="新对话",
            user_id="placeholder_topic_user",
        )
    )

    assert response.topic == "军工装备板块再度走强"
    assert response.focus_object is not None
    assert response.focus_object.text == "军工装备板块再度走强"
    turn = store.last_turn("placeholder_topic_conv", user_id="placeholder_topic_user")
    assert turn["topic"] == "军工装备板块再度走强"


def test_chat_related_reputation_question_answers_under_current_topic(services):
    _, store, _ = services
    search = RecordingSearchService()
    chat = NewsChatService(store, search)

    response = asyncio.run(
        chat.chat(
            "reputation_followup_conv",
            "对于这种美丽却没什么素质的人，网络风评一般是什么走向",
            topic="粗口港姐晒照片 _ 游民星空 GamerSky.com",
            category_scope=["game"],
            user_id="reputation_user",
        )
    )

    assert response.context_relation == "topic_grounded"
    assert response.topic == "粗口港姐晒照片 _ 游民星空 GamerSky.com"
    assert response.focus_object is not None
    assert response.focus_object.text == "粗口港姐晒照片 _ 游民星空 GamerSky.com"
    assert search.calls
    assert "网络风评" in search.calls[0]["query"]
    topics = [item for item in store.list_topics("reputation_user") if item["topic_type"] == "user"]
    assert topics == []
    turn = store.last_turn("reputation_followup_conv", user_id="reputation_user")
    assert turn["topic"] == "粗口港姐晒照片 _ 游民星空 GamerSky.com"


def test_chat_celebrity_privacy_followup_does_not_replace_current_topic(services):
    _, store, _ = services
    search = RecordingSearchService()
    chat = NewsChatService(store, search)
    current_topic = "突发 中国香港演员余文乐官宣离婚：今后仍是家人 _ 游民星空 GamerSky.com"

    response = asyncio.run(
        chat.chat(
            "celebrity_privacy_followup_conv",
            "明星的私生活被过度关注的坏影响",
            topic=current_topic,
            category_scope=["game"],
            user_id="privacy_user",
        )
    )

    assert response.context_relation == "topic_grounded"
    assert response.topic == current_topic
    assert response.focus_object is not None
    assert response.focus_object.text == current_topic
    assert "当前关注主题关联较弱" not in response.answer
    assert search.calls
    assert "明星" in search.calls[0]["query"]
    topics = [item for item in store.list_topics("privacy_user") if item["topic_type"] == "user"]
    assert topics == []
    turn = store.last_turn("celebrity_privacy_followup_conv", user_id="privacy_user")
    assert turn["topic"] == current_topic


def test_topic_agent_creates_from_next_message_after_create_command(services):
    _, store, search = services
    reports = ReportGenerationService(store, search)
    tasks = ScheduledTaskService(store, reports)
    topic_agent = TopicAgentService(store, tasks)
    chat = NewsChatService(store, search, topic_agent=topic_agent)

    skipped = asyncio.run(
        topic_agent.maybe_create_topic_from_chat(
            "explicit_topic_user",
            "帮我关注张雪机车有什么值得关注的新变化",
        )
    )
    assert skipped is None

    pending = asyncio.run(
        chat.chat(
            "explicit_topic_conv",
            "创建一个新的长期专题任务",
            user_id="explicit_topic_user",
        )
    )
    assert pending.context_relation == "topic_create_pending"

    created = asyncio.run(
        chat.chat(
            "explicit_topic_conv",
            "张雪机车有什么值得关注的新变化",
            user_id="explicit_topic_user",
        )
    )
    assert created.context_relation == "topic_agent_created"
    assert created.focus_object.text == "张雪机车"


def test_topic_agent_creates_from_inline_create_command(services):
    _, store, search = services
    reports = ReportGenerationService(store, search)
    tasks = ScheduledTaskService(store, reports)
    topic_agent = TopicAgentService(store, tasks)
    chat = NewsChatService(store, search, topic_agent=topic_agent)

    created = asyncio.run(
        chat.chat(
            "inline_topic_conv",
            "创建一个新的长期专题任务 无锡水蜜桃",
            user_id="inline_topic_user",
        )
    )

    assert created.context_relation == "topic_agent_created"
    assert created.focus_object.text == "无锡水蜜桃"
    topics = [item for item in store.list_topics("inline_topic_user") if item["topic_type"] == "user"]
    assert any(item["title"] == "无锡水蜜桃" for item in topics)


def test_topics_are_scoped_by_conversation(services):
    _, store, search = services
    reports = ReportGenerationService(store, search)
    tasks = ScheduledTaskService(store, reports)
    topic_agent = TopicAgentService(store, tasks)

    asyncio.run(
        topic_agent.create_topic(
            user_id="scoped_topic_user",
            title="无锡水蜜桃",
            conversation_id="topic_conv_a",
            refresh_now=False,
        )
    )
    asyncio.run(
        topic_agent.create_topic(
            user_id="scoped_topic_user",
            title="贵州茅台",
            conversation_id="topic_conv_b",
            refresh_now=False,
        )
    )

    conv_a = [
        item["title"]
        for item in topic_agent.list_topics(
            user_id="scoped_topic_user",
            conversation_id="topic_conv_a",
        )
        if item["topic_type"] == "user"
    ]
    conv_b = [
        item["title"]
        for item in topic_agent.list_topics(
            user_id="scoped_topic_user",
            conversation_id="topic_conv_b",
        )
        if item["topic_type"] == "user"
    ]

    assert conv_a == ["无锡水蜜桃"]
    assert conv_b == ["贵州茅台"]


def test_conversation_turns_round_trip_full_response(services):
    _, store, _ = services
    store.save_turn(
        "conv_history",
        "游戏资讯最近有什么消息？",
        "这里是回答",
        [],
        {"type": "topic", "text": "游戏资讯 消息"},
        user_id="user_history",
        response={"conversation_id": "conv_history", "answer": "这里是回答", "context_relation": "test"},
        topic="游戏资讯 消息",
        category_scope=["game"],
    )
    turns = store.list_turns("conv_history", "user_history")
    assert turns[0]["response"]["answer"] == "这里是回答"
    assert turns[0]["topic"] == "游戏资讯 消息"
    assert turns[0]["category_scope"] == ["game"]


def test_conversation_history_is_grouped_by_user(services):
    _, store, _ = services
    store.save_turn("conv_one", "第一段对话", "回答一", [], None, user_id="history_user", topic="主题一")
    store.save_turn("conv_one", "继续追问", "回答二", [], None, user_id="history_user", topic="主题一")
    store.save_turn("conv_two", "第二段对话", "回答三", [], None, user_id="history_user", topic="主题二")
    store.save_turn("other_conv", "其他用户", "其他回答", [], None, user_id="other_user")

    items = store.list_conversations("history_user")
    by_id = {item["conversation_id"]: item for item in items}
    assert set(by_id) == {"conv_one", "conv_two"}
    assert by_id["conv_one"]["first_message"] == "第一段对话"
    assert by_id["conv_one"]["last_message"] == "继续追问"
    assert by_id["conv_one"]["turn_count"] == 2
    assert by_id["conv_one"]["topic"] == "主题一"


def test_search_prefers_elasticsearch_index(services):
    registry, store, _ = services
    search = UnifiedSearchService(store, registry, search_index=FakeArticleIndex())
    results = asyncio.run(search.search("国际局势 农作物", ["politics"], None, None, 10))
    assert results
    assert results[0].origin == "elasticsearch"
    assert results[0].source_id == "people_politics"


def test_native_source_search_encodes_query(services):
    registry, _, _ = services
    source = registry.get_source("hupu")
    fetcher = FakeLinkFetcher()
    results = asyncio.run(ListPageAdapter(source, fetcher=fetcher).search("机车赛事", limit=2))
    assert results
    assert "%E6%9C%BA%E8%BD%A6%E8%B5%9B%E4%BA%8B" in fetcher.urls[0]
    assert "{query" not in fetcher.urls[0]


def test_search_api_field_matching_supports_nested_paths():
    record = {"article": {"title": "嵌套标题", "url": "https://news.example.com/a.html"}}

    assert _record_value(record, "article.title") == "嵌套标题"
    assert _record_value(record, "article.url") == "https://news.example.com/a.html"
    assert _record_value(record, "missing.title") is None


def test_search_redirect_link_unwraps_targetpage():
    wrapped = "https://search.cctv.com/link_p.php?targetpage=https%3A%2F%2Fsports.cctv.com%2F2026%2F06%2F01%2FARTITest.shtml&point=web"
    assert _unwrap_search_link(wrapped) == "https://sports.cctv.com/2026/06/01/ARTITest.shtml"


def test_article_fetch_service_parses_rss_links():
    fetcher = ArticleFetchService()

    async def fake_get_text(url):
        return """<?xml version="1.0" encoding="utf-8"?>
        <rss><channel>
          <item>
            <title>政策发布新进展</title>
            <link>https://www.gov.cn/zhengce/2026-07/17/content_123.htm</link>
            <pubDate>Fri, 17 Jul 2026 09:30:00 +0800</pubDate>
          </item>
          <item>
            <title>外部站点应被过滤</title>
            <link>https://example.com/a.html</link>
          </item>
        </channel></rss>"""

    fetcher._get_text = fake_get_text
    links = asyncio.run(fetcher.list_rss_links("gov_cn", "policy_latest", "https://www.gov.cn/rss.xml", allowed_domains=["gov.cn"]))
    assert len(links) == 1
    assert links[0].title == "政策发布新进展"
    assert links[0].url == "https://www.gov.cn/zhengce/2026-07/17/content_123.htm"
    assert links[0].published_at.isoformat() == "2026-07-17T01:30:00+00:00"


def test_article_fetch_service_uses_chinanews_mobile_detail_json():
    fetcher = ArticleFetchService()
    urls = []

    async def fake_get_text(url):
        urls.append(url)
        return json.dumps(
            {
                "msgcode": 0,
                "data": {
                    "title": "受紅曲保健品影響 日本小林製藥暫停銷售三款口腔護理產品",
                    "contentNoTag": "中新網9月14日電 綜合日媒報道。",
                    "pubtime": "2024-09-14 11:34:07",
                    "source": "中國新聞網",
                },
            },
            ensure_ascii=False,
        )

    fetcher._get_text = fake_get_text
    article = asyncio.run(
        fetcher.fetch_article(
            "chinanews",
            "https://m.chinanews.com/wap/detail/cht/zwsp/10286135.shtml",
        )
    )

    assert urls == ["https://dw.chinanews.com/cns/app/v1/wapDetail/content/ft10286135.json?language=cht"]
    assert article.title == "受紅曲保健品影響 日本小林製藥暫停銷售三款口腔護理產品"
    assert article.content == "中新網9月14日電 綜合日媒報道。"
    assert article.published_at.isoformat() == "2024-09-14T03:34:07+00:00"


def test_parse_published_datetime_assumes_china_timezone_for_naive_time():
    parsed = _parse_published_datetime("2026年05月18日 10:30")
    assert parsed.isoformat() == "2026-05-18T02:30:00+00:00"


def test_parse_published_datetime_handles_relative_english_time():
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    parsed = _parse_published_datetime("3 days ago", now=now)
    assert parsed.isoformat() == "2026-07-14T12:00:00+00:00"


def test_parse_published_datetime_handles_relative_chinese_time():
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    parsed = _parse_published_datetime("2小时前", now=now)
    assert parsed.isoformat() == "2026-07-17T10:00:00+00:00"


def test_parse_published_datetime_handles_embedded_markdown_time():
    parsed = _parse_published_datetime("# 标题 **2026年07月14日 08:53** 来源")
    assert parsed.isoformat() == "2026-07-14T00:53:00+00:00"


def test_parse_published_datetime_handles_dotted_date():
    parsed = _parse_published_datetime("发布时间：2026.07.14 08:53")
    assert parsed.isoformat() == "2026-07-14T00:53:00+00:00"


def test_extract_published_at_handles_visible_policy_page_date():
    soup = BeautifulSoup(
        """
        <html><body>
          <h1>证监会发布18条政策举措！</h1>
          <div class="article-info">2025‑02‑08 09:10 <span>370</span></div>
        </body></html>
        """,
        "html.parser",
    )

    parsed = _extract_published_at(soup)

    assert parsed.isoformat() == "2025-02-08T01:10:00+00:00"


def test_extract_published_at_handles_publish_time_meta():
    soup = BeautifulSoup(
        '<html><head><meta name="publishTime" content="2025-02-08 09:10"></head></html>',
        "html.parser",
    )

    parsed = _extract_published_at(soup)

    assert parsed.isoformat() == "2025-02-08T01:10:00+00:00"


def test_extract_published_at_prefers_xinhua_visible_full_time_over_date_meta():
    soup = BeautifulSoup(
        """
        <html>
          <head>
            <meta name="source" content="新华网">
            <meta name="publishdate" content="2025-03-26">
          </head>
          <body>
            <div>2025-03-26 22:39:41 来源：新华网</div>
            <h1>粤港澳大湾区应急救援联合演练在港举行</h1>
          </body>
        </html>
        """,
        "html.parser",
    )

    parsed = _extract_published_at(soup)

    assert parsed.isoformat() == "2025-03-26T14:39:41+00:00"


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
            query="机车赛事",
            category_scope=["sports"],
            source_scope=["hupu"],
            max_results=2,
            fetch_articles=1,
        )
    )

    assert payload["discovered_count"] == 1
    assert payload["fetched_count"] == 1
    assert payload["indexed_count"] == 1
    assert fake_index.indexed[0]["title"] == "机车赛事更新"
    saved = store.search_articles("机车赛事", ["sports"], limit=5)
    assert saved and saved[0]["source_id"] == "hupu"


def test_deep_dive_generates_expansion_queries_and_evidence(services):
    registry, store, _ = services
    search = UnifiedSearchService(store, registry)
    payload = asyncio.run(DeepDiveService(search).run("国际局势 农作物", ["politics"], None, rounds=1, breadth=2))
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


def test_event_cluster_title_uses_real_clean_headline_instead_of_weak_keyword():
    title = _cluster_title(
        "10",
        [
            {"title": "[流言板] WTT欧洲大满贯迎来关键比赛-腾讯新闻"},
            {"title": "另一条较长的体育新闻标题用于测试聚合标题展示-虎扑"},
        ],
    )

    assert "相关热点" not in title
    assert "流言板" not in title
    assert not title.endswith("腾讯新闻")


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
    updated = scheduler.due_plan(category="tech", limit=50)
    same = [item for item in updated["sections"] if item["source_id"] == first["source_id"] and item["section_key"] == first["section_key"]]
    assert same and same[0]["due"] is False


def test_chat_resolves_second_article_followup(services):
    _, store, search = services
    chat = NewsChatService(store, search)
    first = asyncio.run(chat.chat("conv_test", "车型", category_scope=["auto"]))
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
    assert "provider" not in report.sections["八、来源列表与不确定性说明"].lower()
    with store.connect() as conn:
        operations = [row["operation"] for row in conn.execute("SELECT operation FROM operation_logs").fetchall()]
    assert "news_search" in operations
    assert "report_generation" in operations


def test_daily_digest_uses_local_agent_when_available(services):
    _, store, search = services
    local_agent = FakeBriefLocalAgent()
    reports = ReportGenerationService(store, search, local_agent=local_agent)
    report = asyncio.run(reports.generate("default", "今日资讯", ["tech"], "1d", "daily_digest"))

    assert local_agent.calls == 1
    assert report.sections["generation_source"] == "local_agent"
    assert report.sections["headline"] == "AI 今日简报"
    assert report.sections["top_stories"][0]["title"] == "AI Agent 产品更新"
    assert report.sources


def test_chat_executes_brief_skill_with_local_agent(services):
    registry, store, search = services
    local_agent = FakeBriefLocalAgent()
    reports = ReportGenerationService(store, search, local_agent=local_agent)
    chat = NewsChatService(
        store,
        search,
        local_agent=local_agent,
        skill_registry=build_default_registry(),
        services={"reports": reports, "registry": registry},
    )
    response = asyncio.run(chat.chat("brief_conv", "/brief --category tech", user_id="default"))

    assert response.context_relation == "skill:/brief"
    assert response.skill_result["command"] == "/brief"
    assert response.skill_result["data"]["sections"]["generation_source"] == "local_agent"
    assert "AI 今日简报" in response.answer
    assert store.last_turn("brief_conv")["response"]["context_relation"] == "skill:/brief"


def test_brief_fallback_uses_human_importance_not_source_noise(services):
    registry, store, search = services
    reports = ReportGenerationService(store, search)
    chat = NewsChatService(
        store,
        search,
        skill_registry=build_default_registry(),
        services={"reports": reports, "registry": registry},
    )

    response = asyncio.run(chat.chat("brief_fallback_conv", "/brief --category tech", user_id="default"))

    assert response.context_relation == "skill:/brief"
    assert "匹配当前主题或用户偏好" not in response.answer
    assert "\n- -36" not in response.answer
    assert "\n- GamerSky.com" not in response.answer
    assert "本次简报汇总了" in response.answer


def test_brief_story_summaries_drop_unrelated_recommendation_text(services):
    registry, store, search = services
    store.save_article(
        NormalizedArticle(
            id="sports_noise_brief",
            source_id="chinanews",
            section_key="sports",
            url="https://example.com/sports-noise",
            title="覃予萱/蒯曼夺得2026年乒乓球全锦赛女双冠军-中新网",
            summary=(
                "7月19日晚，2026年乒乓球全国锦标赛女双决赛中，覃予萱/蒯曼以3比0获胜，首次夺得全锦赛女双冠军。"
                "当AI拥有“终身学习”能力，人类如何划定“不可逾越之界”？ "
                "APP借钱套路调查：收个红包、付笔账单、点个优惠，贷款就背上了"
            ),
            content="",
            category="sports",
            published_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            fetched_at=datetime.now(timezone.utc),
            source_priority=1,
            keywords=[],
            entities=[],
            content_hash=content_hash("sports_noise_brief"),
        )
    )
    reports = ReportGenerationService(store, search)
    chat = NewsChatService(
        store,
        search,
        skill_registry=build_default_registry(),
        services={"reports": reports, "registry": registry},
    )

    response = asyncio.run(chat.chat("brief_noise_conv", "/brief --category sports", user_id="default"))

    assert response.context_relation == "skill:/brief"
    assert "覃予萱/蒯曼以3比0获胜，首次夺得全锦赛女双冠军" in response.answer
    assert "当AI拥有" not in response.answer
    assert "APP借钱套路调查" not in response.answer


def test_brief_story_summary_keeps_complete_sentences_without_hard_cutoff():
    long_summary = (
        "基本框架是：库明加去湖人，老鹰得到贾里德·范德比尔特，加上湖人2032年首轮签互换权。"
        "2023年9月，范德比尔特与湖人签下一份多年合同，因此他的合同处理、健康状况和第三方球队接手意愿都会影响交易可行性。"
        "后续需要观察老鹰、湖人和潜在第三方球队是否出现更明确报价。"
    )

    summary = _brief_story_summary("库明加先签后换传闻", long_summary, "")

    assert summary.endswith("后续需要观察老鹰、湖人和潜在第三方球队是否出现更明确报价。")
    assert "2023年9月，范 " not in summary
    assert "交易可行性。" in summary


def test_brief_overview_summary_uses_complete_sentence_summaries():
    evidence = [
        {
            "index": 1,
            "title": "20相关热点：普京签署总统令延长对中国公民免签政策-中新网",
            "summary": "中新社莫斯科12月1日电俄罗斯总统普京12月1日签署命令，在2026年9月14日前，中国公民可免签入境俄罗斯并最长停留30日。俄总统网站当天发布的相关法令称，政策适用于探亲、商务、旅游、过境等目的。",
            "content_excerpt": "",
        },
        {
            "index": 2,
            "title": "俄罗斯将延长对中国公民的免签制度*中国机电产品进出口商会",
            "summary": "信息资讯。来源：央视新闻 时间：2026-05-21。据俄罗斯总统普京去年12月1日签署的命令，中国公民可免办签证进入俄罗斯并停留不超过30天。",
            "content_excerpt": "",
        },
    ]

    sections = _fallback_brief_sections("普京签署总统令延长对中国公民免签政策", ["politics"], {}, evidence)

    assert sections["summary"].endswith("中国公民可免办签证进入俄罗斯并停留不超过30天。")
    assert "202..." not in sections["summary"]
    assert "### 信息资讯" not in sections["summary"]
    assert sections["summary"].count("。") <= 3


def test_brief_without_inline_topic_uses_latest_conversation_topic(services):
    registry, store, _ = services
    search = RecordingSearchService()
    reports = ReportGenerationService(store, search)
    chat = NewsChatService(
        store,
        search,
        skill_registry=build_default_registry(),
        services={"reports": reports, "registry": registry},
    )

    first = asyncio.run(chat.chat("brief_context_conv", "追踪汽车之家是否出现后续回应", category_scope=["auto"], user_id="default"))
    search.calls.clear()
    response = asyncio.run(
        chat.chat(
            "brief_context_conv",
            "/brief",
            topic="今日资讯",
            category_scope=["tech"],
            user_id="default",
        )
    )

    assert first.topic == "追踪汽车之家是否出现后续回应"
    assert response.context_relation == "skill:/brief"
    assert response.skill_result["data"]["topic"] == "追踪汽车之家是否出现后续回应"
    assert response.skill_result["data"]["category_scope"] == ["auto"]
    assert search.calls == []
    assert response.skill_result["data"]["sources"][0]["title"] == "追踪汽车之家是否出现后续回应 报道"
    assert "今日简报：追踪汽车之家是否出现后续回应" in response.answer


def test_brief_uses_existing_conversation_content_without_new_search(services):
    registry, store, _ = services
    class TopicUrlSearchService(RecordingSearchService):
        async def search(self, query, category_scope, source_scope, time_range, max_results=20, include_remote=False):
            self.calls.append(
                {
                    "query": query,
                    "category_scope": category_scope,
                    "source_scope": source_scope,
                    "time_range": time_range,
                    "include_remote": include_remote,
                }
            )
            return [
                SearchResult(
                    source_id="test",
                    title=f"{query} 报道",
                    url=f"https://example.com/{stable_id('brief', query)}",
                    summary=f"{query} 的摘要",
                    category=(category_scope or ["all"])[0],
                    published_at=datetime.now(timezone.utc),
                    score=1.0,
                    origin="local",
                )
            ]

    search = TopicUrlSearchService()
    reports = ReportGenerationService(store, search)
    chat = NewsChatService(
        store,
        search,
        skill_registry=build_default_registry(),
        services={"reports": reports, "registry": registry},
    )

    asyncio.run(chat.chat("conversation_brief_conv", "重男轻女相关政策争议", category_scope=["politics"], user_id="default"))
    asyncio.run(chat.chat("conversation_brief_conv", "男女比例和历史事件有什么关系", category_scope=["politics"], user_id="default"))
    search.calls.clear()
    response = asyncio.run(chat.chat("conversation_brief_conv", "/brief", topic="重男轻女", user_id="default"))

    assert search.calls == []
    assert response.context_relation == "skill:/brief"
    assert response.skill_result["data"]["topic"] == "重男轻女"
    assert response.skill_result["data"]["sections"]["generation_source"] == "conversation_fallback"
    titles = [item["title"] for item in response.skill_result["data"]["sources"]]
    assert any("重男轻女相关政策争议" in title for title in titles)
    assert any("男女比例和历史事件" in title for title in titles)
    assert "简报阶段未新增检索" in response.answer


def test_brief_includes_turn_forced_related_by_user(services):
    registry, store, _ = services

    class UniqueUrlSearchService(RecordingSearchService):
        async def search(self, query, category_scope, source_scope, time_range, max_results=20, include_remote=False):
            self.calls.append(
                {
                    "query": query,
                    "category_scope": category_scope,
                    "source_scope": source_scope,
                    "time_range": time_range,
                    "include_remote": include_remote,
                }
            )
            return [
                SearchResult(
                    source_id="test",
                    title=f"{query} 报道",
                    url=f"https://example.com/{stable_id('brief_forced', query)}",
                    summary=f"{query} 的摘要",
                    category=(category_scope or ["all"])[0],
                    published_at=datetime.now(timezone.utc),
                    score=1.0,
                    origin="local",
                )
            ]

    search = UniqueUrlSearchService()
    reports = ReportGenerationService(store, search)
    chat = NewsChatService(
        store,
        search,
        skill_registry=build_default_registry(),
        services={"reports": reports, "registry": registry},
    )

    first = asyncio.run(chat.chat("forced_related_brief_conv", "张雪机车最新进展", category_scope=["sports"], user_id="default"))
    second = asyncio.run(chat.chat("forced_related_brief_conv", "军工装备板块再度走强", topic=first.topic, category_scope=["sports"], user_id="default"))
    assert "当前关注主题关联较弱" in second.answer
    second_turn = store.last_turn("forced_related_brief_conv", user_id="default")
    store.set_turn_relation(second_turn["id"], "default", "related", chat.topic_drift_notice)

    response = asyncio.run(chat.chat("forced_related_brief_conv", "/brief", topic=first.topic, user_id="default"))

    titles = [item["title"] for item in response.skill_result["data"]["sources"]]
    assert any("张雪机车" in title for title in titles)
    assert any("军工装备板块再度走强" in title for title in titles)


def _assert_fixed_report_modules(answer: str):
    modules = [
        "### 一句话结论",
        "### 覆盖范围",
        "### 发生了什么",
        "### 关键证据",
        "### 各方说法",
        "### 为什么重要",
        "### 争议与不确定性",
        "### 后续观察点",
        "### 时间线",
        "### 证据来源列表",
    ]
    positions = [answer.index(module) for module in modules]
    assert positions == sorted(positions)


def test_report_marks_truncated_excerpts_instead_of_dangling_ellipsis():
    answer = _report_skill_answer(
        "专题报告：工信部利润率…",
        "报告已生成。",
        {
            "report_id": "rpt_test",
            "topic": "工信部利润率…",
            "category_scope": ["tech"],
            "sections": {
                "一、结论摘要": "工信部相关数据需要结合统计局历史口径一起看…",
                "二、事件背景": "对话内讨论了 2024 年和 2026 年规模以上工业企业营业收入利润率差异…",
                "六、不同来源的主要说法": [
                    {
                        "source_id": "kr36",
                        "title": "工信部：前5个月规模以上工业企业营业收入利润率5.66% 为2024年以来月度累计最高水平-36氪…",
                        "summary": "2024年1—5月份全国规模以上工业企业利润增长3.4%。1—5月份，规模以上工业企业实现营业收入53.03万亿元，同比增长2.9%…",
                    }
                ],
                "八、来源列表与不确定性说明": "对话内材料可能只是摘录…",
            },
            "timeline": [{"date": "2026-07-20", "event": "工信部发布相关利润率数据…"}],
            "sources": [
                {
                    "source_id": "kr36",
                    "title": "工信部：前5个月规模以上工业企业营业收入利润率5.66% 为2024年以来月度累计最高水平-36氪…",
                    "url": "https://36kr.com/newsflashes/test",
                }
            ],
        },
    )

    assert "工信部利润率…" not in answer
    assert "文章标题.…" not in answer
    assert "（已截断，仅展示对话内摘录）" in answer
    assert "2026-07-20：工信部发布相关利润率数据…" not in answer
    _assert_fixed_report_modules(answer)


def test_report_export_filters_keyword_fragments_from_reader_sections():
    exported = export_report(
        {
            "report_id": "rpt_noise",
            "topic": "同花顺：董事长提议实施2026年中期利润分配方案，拟10派2元-36氪",
            "category_scope": ["economy"],
            "sections": {
                "一、结论摘要": (
                    "同花顺：董事长提议实施2026年中期利润分配方案，拟10派2元-36氪。"
                    "燕京啤酒：2025年度每10股派2元 - 财经- 同花顺。"
                    "中文传媒2025年全年每10股派2元股权登记日为2026年6月11日。"
                    "同益中2025年全年每10股派1元股权登记日为2026年6月23日。"
                    "同花顺股票频道—提供最新的A股上市公司新闻、行情、公告及资讯。"
                ),
                "五、主要争议点/看点": ["10", "2025", "2026", "同花顺", "亿元"],
                "六、不同来源的主要说法": [
                    {
                        "source_id": "news.10jqka.com.cn",
                        "title": "燕京啤酒：2025年度每10股派2元 - 财经- 同花顺",
                        "summary": "这是燕京啤酒公告。",
                    },
                    {
                        "source_id": "36kr",
                        "title": "同花顺：董事长提议实施2026年中期利润分配方案，拟10派2元-36氪",
                        "summary": "标题指向同花顺利润分配方案。",
                    },
                ],
                "七、可能影响与后续观察指标": ["10", "2025", "2026"],
            },
            "timeline": [
                {"date": "2026-07-20", "event": "燕京啤酒：2025年度每10股派2元 - 财经- 同花顺"},
                {"date": "2026-07-20", "event": "同花顺：董事长提议实施2026年中期利润分配方案"},
            ],
            "sources": [
                {"source_id": "news.10jqka.com.cn", "title": "燕京啤酒：2025年度每10股派2元 - 财经- 同花顺", "url": "https://example.com/a"},
                {"source_id": "36kr", "title": "同花顺：董事长提议实施2026年中期利润分配方案，拟10派2元-36氪", "url": "https://example.com/b"},
            ],
        },
        "docx",
    )

    with zipfile.ZipFile(BytesIO(exported.content)) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")

    assert "<w:t>10</w:t>" not in document_xml
    assert "<w:t>2025</w:t>" not in document_xml
    assert "<w:t>同花顺</w:t>" not in document_xml
    assert "当前报告围绕" in document_xml
    assert "避免把来源名、年份或金额碎片误当成结论" in document_xml
    assert "继续查找原始公告" in document_xml
    assert "燕京啤酒" not in document_xml
    assert "标题指向同花顺利润分配方案" in document_xml


def test_report_export_keeps_full_text_while_web_bubble_truncates():
    long_summary = "这是完整报告正文。" + "用于验证下载文件不应截断的长段落，" * 45 + "末尾保留完整结论。"
    source_full_text = "这是来源完整正文。" + "导出文件应优先采用 sources 里的完整内容，" * 40 + "来源正文末尾也必须保留。"
    payload = {
        "report_id": "rpt_full_export",
        "topic": "绿色家电主题宣传月",
        "category_scope": ["economy"],
        "sections": {
            "一、结论摘要": "绿色家电主题宣传月围绕消费政策、企业动作和后续补贴执行展开。",
            "二、事件背景": long_summary,
            "六、不同来源的主要说法": [
                {
                    "source_id": "test",
                    "title": "绿色家电主题宣传月在青岛启动",
                    "summary": "短摘要",
                }
            ],
            "五、主要争议点/看点": ["政策执行节奏、企业参与范围和消费者实际获得感仍需继续观察。"],
        },
        "timeline": [{"date": "2026-07-20", "event": "绿色家电主题宣传月在青岛启动"}],
        "sources": [{"source_id": "test", "title": "绿色家电主题宣传月在青岛启动", "url": "https://example.com/green", "full_text": source_full_text}],
    }

    bubble = _report_skill_answer("专题报告：绿色家电主题宣传月", "报告已生成。", payload)
    exported = export_report(payload, "docx")
    with zipfile.ZipFile(BytesIO(exported.content)) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")

    assert "（已截断，仅展示对话内摘录）" in bubble
    assert "末尾保留完整结论" not in bubble
    assert "（已截断" not in document_xml
    assert "末尾保留完整结论" in document_xml
    assert "来源正文末尾也必须保留" in document_xml


def test_conversation_report_export_lists_all_scoped_sources():
    sources = [
        {
            "article_id": f"art_{index}",
            "source_id": "kr36",
            "title": f"WAIC 2026 相关证据 {index}",
            "url": f"https://example.com/{index}",
            "summary": f"第 {index} 条完整证据摘要。",
            "full_text": f"第 {index} 条完整证据正文。",
        }
        for index in range(1, 29)
    ]
    payload = {
        "report_id": "rpt_all_sources",
        "topic": "在WAIC现场，我们见到了Richard Sutton-36氪",
        "report_type": "conversation_summary",
        "conversation_id": "conv_report_all_sources",
        "category_scope": ["tech"],
        "sections": {
            "一、结论摘要": "围绕 Richard Sutton 的 WAIC 现场报道整理。",
            "二、事件背景": "共覆盖 28 条已展示证据。",
            "六、不同来源的主要说法": sources[:1],
        },
        "timeline": [{"date": "2026-07-20", "event": item["title"]} for item in sources],
        "sources": sources,
    }

    exported = export_report(payload, "docx")
    with zipfile.ZipFile(BytesIO(exported.content)) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")

    assert "[28]" in document_xml
    assert "WAIC 2026 相关证据 28" in document_xml
    assert "第 28 条完整证据正文" in document_xml


def test_report_topic_filter_ignores_source_name_only_matches():
    topic = "同花顺：董事长提议实施2026年中期利润分配方案，拟10派2元-36氪"
    evidence = [
        {
            "title": "燕京啤酒：2025年度每10股派2元 - 财经- 同花顺",
            "summary": "燕京啤酒公告，2025年度利润分配方案为每10股派2元。",
        },
        {
            "title": "同花顺：2026年中期利润分配方案，拟10派2元",
            "summary": "同花顺董事长提议实施中期利润分配方案。",
        },
    ]

    filtered = _filter_conversation_evidence_by_topic(evidence, topic)

    assert [item["title"] for item in filtered] == ["同花顺：2026年中期利润分配方案，拟10派2元"]


def test_chat_executes_report_skill_as_full_topic_summary(services):
    registry, store, search = services
    reports = ReportGenerationService(store, search)
    chat = NewsChatService(
        store,
        search,
        skill_registry=build_default_registry(),
        services={"reports": reports, "registry": registry},
    )

    response = asyncio.run(
        chat.chat(
            "report_conv",
            "/report",
            topic="新能源汽车价格战",
            category_scope=["auto", "economy"],
            user_id="default",
        )
    )

    assert response.context_relation == "skill:/report"
    assert response.skill_result["command"] == "/report"
    assert response.skill_result["data"]["topic"] == "新能源汽车价格战"
    assert "专题报告：新能源汽车价格战" in response.answer
    _assert_fixed_report_modules(response.answer)
    assert "新能源汽车价格战进入新阶段" in response.answer
    assert "provider" not in response.answer.lower()


def test_report_uses_existing_conversation_content_without_new_search(services):
    registry, store, _ = services
    search = RecordingSearchService()
    reports = ReportGenerationService(store, search)
    chat = NewsChatService(
        store,
        search,
        skill_registry=build_default_registry(),
        services={"reports": reports, "registry": registry},
    )

    first = asyncio.run(chat.chat("conversation_report_conv", "绿色家电企业有哪些动作", user_id="default"))
    search.calls.clear()
    response = asyncio.run(chat.chat("conversation_report_conv", "/report", user_id="default"))

    assert first.recommendations
    assert search.calls == []
    assert response.context_relation == "skill:/report"
    assert response.skill_result["data"]["topic"] == "绿色家电企业有哪些动作"
    assert response.skill_result["data"]["sources"][0]["title"] == "绿色家电企业有哪些动作 报道"
    assert "本报告只整理当前对话" in response.answer
    assert "报告阶段未新增检索" in response.answer
    _assert_fixed_report_modules(response.answer)


def test_report_after_brief_uses_structured_stories_not_raw_markdown(services):
    registry, store, search = services
    reports = ReportGenerationService(store, search)
    chat = NewsChatService(
        store,
        search,
        skill_registry=build_default_registry(),
        services={"reports": reports, "registry": registry},
    )

    brief = asyncio.run(chat.chat("report_after_brief_conv", "/brief --category tech", user_id="default"))
    report = asyncio.run(chat.chat("report_after_brief_conv", "/report", user_id="default"))

    assert brief.context_relation == "skill:/brief"
    assert report.context_relation == "skill:/report"
    assert report.skill_result["data"]["sources"]
    assert "## 今日简报" not in report.answer
    assert "/brief（对话回答）" not in report.answer
    assert "相关证据：0 条" not in report.answer
    _assert_fixed_report_modules(report.answer)


def test_report_excludes_unrelated_conversation_content(services):
    registry, store, _ = services
    search = RecordingSearchService()
    reports = ReportGenerationService(store, search)
    chat = NewsChatService(
        store,
        search,
        skill_registry=build_default_registry(),
        services={"reports": reports, "registry": registry},
    )

    asyncio.run(chat.chat("mixed_report_conv", "绿色家电企业有哪些动作", user_id="default"))
    asyncio.run(chat.chat("mixed_report_conv", "大型体育赛事运营有什么新闻", user_id="default"))
    search.calls.clear()
    response = asyncio.run(chat.chat("mixed_report_conv", "/report 绿色家电企业有哪些动作", user_id="default"))

    assert search.calls == []
    assert response.context_relation == "skill:/report"
    assert "绿色家电企业有哪些动作 报道" in response.answer
    assert "大型体育赛事运营 报道" not in response.answer
    assert len(response.skill_result["data"]["sources"]) == 1
    assert response.skill_result["data"]["sources"][0]["title"] == "绿色家电企业有哪些动作 报道"
    assert "明显偏离主题的内容已排除" in response.answer
    _assert_fixed_report_modules(response.answer)


def test_bare_report_keeps_original_conversation_topic_and_includes_expansions(services):
    registry, store, _ = services
    class TopicExpansionSearchService(RecordingSearchService):
        async def search(self, query, category_scope, source_scope, time_range, max_results=20, include_remote=False):
            self.calls.append(
                {
                    "query": query,
                    "category_scope": category_scope,
                    "source_scope": source_scope,
                    "time_range": time_range,
                    "include_remote": include_remote,
                }
            )
            return [
                SearchResult(
                    source_id="test",
                    title=f"{query} 报道",
                    url=f"https://example.com/{stable_id('q', query)}",
                    summary=f"{query} 的摘要",
                    category=(category_scope or ["all"])[0],
                    published_at=datetime.now(timezone.utc),
                    score=1.0,
                    origin="local",
                )
            ]

    search = TopicExpansionSearchService()
    reports = ReportGenerationService(store, search)
    chat = NewsChatService(
        store,
        search,
        skill_registry=build_default_registry(),
        services={"reports": reports, "registry": registry},
    )
    original_topic = "几十年的游戏账号说删就删！数字所有权就是个笑话？ _ 游民星空 GamerSky.com"

    first = asyncio.run(chat.chat("game_ownership_report_conv", f"基于资讯“{original_topic}”继续深挖，给我结论、证据和后续观察点。", category_scope=["game"], user_id="default"))
    second = asyncio.run(chat.chat("game_ownership_report_conv", "用户与游戏公司起冲突的案例", category_scope=["game"], user_id="default"))
    third = asyncio.run(chat.chat("game_ownership_report_conv", "有哪些游戏与玩家的冲突闹上了法庭呢", category_scope=["game"], user_id="default"))
    response = asyncio.run(chat.chat("game_ownership_report_conv", "/report", user_id="default"))

    assert first.topic == original_topic
    assert second.topic == original_topic
    assert third.topic == original_topic
    assert response.context_relation == "skill:/report"
    assert response.skill_result["data"]["topic"] == original_topic
    titles = [item["title"] for item in response.skill_result["data"]["sources"]]
    assert any("用户与游戏公司起冲突" in title for title in titles)
    assert any("冲突闹上了法庭" in title for title in titles)
    assert "共覆盖 3 轮相关对话" in response.answer
    _assert_fixed_report_modules(response.answer)


def test_factcheck_uses_local_agent_verdict(services):
    _, store, search = services
    local_agent = FakeFactCheckLocalAgent()
    factcheck = FactCheckService(store, search, local_agent=local_agent)

    result = asyncio.run(factcheck.run("default", "AI Agent 产品更新带动开发工具竞争", ["tech"]))

    assert local_agent.calls == 1
    assert result.agent_source == "local_agent"
    assert result.verdict == "supported"
    assert result.confidence == 0.82
    assert result.supporting_evidence
    assert result.supporting_evidence[0]["index"] == 1


def test_factcheck_falls_back_when_agent_fails(services):
    _, store, search = services
    factcheck = FactCheckService(store, search, local_agent=FakeFailingLocalAgent())

    result = asyncio.run(factcheck.run("default", "AI Agent 产品更新带动开发工具竞争", ["tech"]))

    assert result.agent_source == "fallback"
    assert result.verdict == "insufficient"
    assert result.evidence
    assert any("外部搜索工具" in note for note in result.source_notes)


def test_factcheck_falls_back_to_structured_llm_summary(services):
    _, store, search = services
    llm = FakeFactCheckLLM()
    factcheck = FactCheckService(
        store,
        search,
        local_agent=FakeFailingLocalAgent(),
        llm_client=llm,
    )

    result = asyncio.run(factcheck.run("default", "AI Agent 产品更新带动开发工具竞争", ["tech"]))

    assert llm.calls == 1
    assert result.agent_source == "llm"
    assert result.verdict == "supported"
    assert result.supporting_evidence
    assert [item["index"] for item in result.supporting_evidence] == [1]


def test_factcheck_merge_reserves_web_results_and_deduplicates_canonical_urls():
    local = [
        SearchResult(
            source_id="local",
            title=f"本地证据 {index}",
            url=f"https://example.com/news/{index}?utm_source=feed",
            summary="本地摘要",
            category="tech",
            published_at=datetime.now(timezone.utc),
            score=1.0,
            origin="local",
        )
        for index in range(1, 5)
    ]
    web = [
        SearchResult(
            source_id="web",
            title="重复证据",
            url="https://example.com/news/1?utm_campaign=search",
            summary="联网摘要",
            category="tech",
            published_at=datetime.now(timezone.utc),
            score=0.8,
            origin="external",
        ),
        SearchResult(
            source_id="web",
            title="独立联网证据",
            url="https://web.example/factcheck",
            summary="联网摘要",
            category="tech",
            published_at=datetime.now(timezone.utc),
            score=0.8,
            origin="external",
        ),
    ]

    merged = _merge_search_results(local, web, 4)

    assert len(merged) == 4
    assert sum(item.origin == "external" for item in merged) == 1
    assert any(item.url == "https://web.example/factcheck" for item in merged)


def test_factcheck_contradicted_verdict_does_not_repeat_same_source_as_supporting():
    evidence = [
        {
            "index": 1,
            "title": "IPO仍在推介阶段",
            "source_id": "source",
            "url": "https://example.com/ipo-stage",
            "summary": "尚未完成上市。",
        }
    ]
    parsed = {
        "verdict": "contradicted",
        "confidence": 0.9,
        "summary": "该说法与现有流程状态矛盾。",
        "supporting_evidence": [1],
        "contradicting_evidence": [1],
        "missing_evidence": [],
        "source_notes": [],
        "next_checks": [],
    }
    fallback = {
        "verdict": "insufficient",
        "confidence": 0.2,
        "summary": "证据不足。",
        "supporting_evidence": [],
        "contradicting_evidence": [],
        "missing_evidence": ["官方公告"],
        "source_notes": ["本地证据。"],
        "next_checks": ["查官方公告"],
    }

    normalized = _normalize_payload(parsed, fallback, evidence)

    assert normalized["supporting_evidence"] == []
    assert normalized["contradicting_evidence"][0]["url"] == "https://example.com/ipo-stage"


def test_chat_executes_factcheck_skill_with_local_agent(services):
    registry, store, search = services
    local_agent = FakeFactCheckLocalAgent()
    factcheck = FactCheckService(store, search, local_agent=local_agent)
    chat = NewsChatService(
        store,
        search,
        local_agent=local_agent,
        skill_registry=build_default_registry(),
        services={"factcheck": factcheck, "registry": registry},
    )

    response = asyncio.run(
        chat.chat(
            "factcheck_conv",
            "/factcheck AI Agent 产品更新带动开发工具竞争 --category tech",
            user_id="default",
        )
    )

    assert response.context_relation == "skill:/factcheck"
    assert response.skill_result["command"] == "/factcheck"
    assert response.skill_result["data"]["agent_source"] == "local_agent"
    assert response.skill_result["data"]["verdict"] == "supported"
    assert "结论：supported" in response.answer
    assert "支持证据" in response.answer
    assert "下一步核查" in response.answer


def test_factcheck_skill_always_uses_direct_web_search(services):
    _, store, _ = services
    search = RecordingSearchService()
    factcheck = FactCheckService(store, search, local_agent=FakeFailingLocalAgent())
    chat = NewsChatService(
        store,
        search,
        skill_registry=build_default_registry(),
        services={"factcheck": factcheck},
    )

    response = asyncio.run(
        chat.chat(
            "factcheck_remote_conv",
            "/factcheck AI Agent 产品更新带动开发工具竞争 --category tech",
            user_id="default",
            allow_web_search=False,
        )
    )

    assert response.context_relation == "skill:/factcheck"
    assert search.calls[0]["include_remote"] is False
    assert search.external_calls
    assert any("外部搜索工具返回 1 条" in note for note in response.skill_result["data"]["source_notes"])


def test_factcheck_prefers_cc_project_skill_and_maps_url_evidence(services):
    _, store, search = services
    runtime = FakeProjectSkillCCRuntime()
    factcheck = FactCheckService(store, search, cc_runtime=runtime)

    result = asyncio.run(factcheck.run("default", "AI Agent 产品更新带动开发工具竞争", ["tech"]))

    assert result.agent_source == "cc_runtime"
    assert result.verdict == "supported"
    assert result.supporting_evidence[0]["url"] == "https://runtime.example/factcheck"
    assert runtime.skill_names == ["news-fact-check"]
    assert any(item["stage"] == "事实核查" and item["status"] == "completed" for item in result.research_trace)


def test_hot_event_map_skill_returns_mermaid_markdown_and_stream_trace(services):
    _, store, search = services
    runtime = FakeProjectSkillCCRuntime()
    chat = NewsChatService(
        store,
        search,
        skill_registry=build_default_registry(),
        services={"cc_runtime": runtime, "search": search},
    )

    async def collect_events():
        return [
            event
            async for event in chat.chat_events(
                "map_skill_conv",
                "/map AI Agent 产品更新 --category tech",
                user_id="default",
                allow_web_search=True,
            )
        ]

    events = asyncio.run(collect_events())
    response = events[-1]["response"]

    assert response["context_relation"] == "skill:/map"
    assert "```mermaid" in response["markdown"]
    assert response["skill_result"]["data"]["agent_source"] == "cc_runtime"
    assert runtime.skill_names == ["hot-event-map"]
    assert any(event["type"] == "trace" and event["item"]["stage"] == "本地新闻引擎" for event in events)


def test_hot_event_map_sanitizes_mermaid_html_and_interaction_directives():
    markdown = "## 事件图谱\n\n```mermaid\nflowchart LR\nA[\"节点<br/>下一行\"]\nclick A javascript:alert(1)\n```"

    cleaned = _sanitize_event_map_markdown(markdown)

    assert "<br" not in cleaned
    assert "click A" not in cleaned
    assert "javascript:" not in cleaned
    assert "节点 · 下一行" in cleaned


def test_hot_event_map_repairs_transposed_quoted_edge_label_from_cc():
    markdown = (
        '## 事件图谱\n\n```mermaid\nflowchart LR\n'
        'VALVE["Valve"]\nTI14["TI14"]\n'
        'VALVE -->|"未公布举办地|" TI14\n```'
    )

    cleaned = _sanitize_event_map_markdown(markdown)

    assert 'VALVE -->|"未公布举办地"| TI14' in cleaned
    assert '|"未公布举办地|"' not in cleaned


def test_report_cleans_polluted_factcheck_title(services):
    registry, store, search = services
    reports = ReportGenerationService(store, search)
    chat = NewsChatService(
        store,
        search,
        skill_registry=build_default_registry(),
        services={"reports": reports, "registry": registry},
    )

    response = asyncio.run(
        chat.chat(
            "polluted_report_conv",
            "/report 事实核查：继续核查：国际粮食安全议题引发多方关注 --category politics",
            user_id="default",
        )
    )

    assert response.context_relation == "skill:/report"
    assert response.skill_result["data"]["topic"] == "国际粮食安全议题引发多方关注"
    assert "专题报告：国际粮食安全议题引发多方关注" in response.answer
    assert "专题报告：事实核查" not in response.answer
    assert response.skill_result["data"]["sources"]


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
            "topics": ["机车赛事"],
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


def test_topic_summary_skill_generates_structured_markdown(services):
    _, store, search = services
    service = TopicSummaryService(store, search, llm=type("FakeLLM", (), {"configured": False})())
    payload = asyncio.run(
        service.generate(
            user_id="default",
            topic="新能源汽车价格战",
            category_scope=["auto", "economy"],
            max_articles=8,
            use_llm=False,
        )
    )

    assert payload["report_id"].startswith("rpt_")
    assert payload["summary"]["sections"]
    assert payload["summary"]["timeline"]
    assert payload["summary"]["graph"]["nodes"]
    assert "## 时间线" in payload["markdown"]
    assert "## 人物/事件图谱" in payload["markdown"]


def test_topic_summary_skill_accepts_llm_structured_output(services):
    _, store, search = services
    service = TopicSummaryService(store, search, llm=FakeTopicSummaryLLM())
    payload = asyncio.run(
        service.generate(
            user_id="default",
            topic="新能源汽车价格战",
            category_scope=["auto"],
            max_articles=4,
            use_llm=True,
        )
    )

    assert payload["summary"]["title"] == "新能源汽车价格战专题"
    assert payload["summary"]["graph"]["edges"][0]["label"] == "影响"
    assert payload["summary"]["sections"][0]["evidence_indices"] == [1]


def test_topic_summary_schema_requires_every_output_field():
    schema = TopicSummaryOutput.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_schedule_command_creates_push_and_appends_conversation(services):
    _, store, search = services
    reports = ReportGenerationService(store, search)
    fake_llm = type("FakeLLM", (), {"configured": False})()
    tasks = ScheduledTaskService(store, reports, llm_client=fake_llm)

    created = asyncio.run(
        tasks.create_from_schedule_message(
            "schedule_user",
            "/schedule 帮我定时每天早晨9点收集关于AI Agent的新闻，并总结成一个专题发给我",
        )
    )

    assert created["task"]["task_type"] == "scheduled_push"
    assert created["task"]["schedule"] == "0 9 * * *"
    assert created["task"]["topics"] == ["AI Agent"]
    assert created["task"]["raw_task_description"].startswith("/schedule")
    assert created["task"]["parsed_workflow"]["report_style"]["sections"]
    assert created["conversation"]["kind"] == "scheduled_push"
    turns = store.list_turns(created["conversation"]["id"], user_id="schedule_user")
    assert turns[-1]["payload"]["type"] == "scheduled_task_created"
    assert turns[-1]["payload"]["api_params"]["parsed_workflow"]["fetch_strategy"]["max_sources"] == 3

    result = asyncio.run(tasks.run_task(created["task"]["id"]))
    assert result["status"] == "ok"
    assert result["conversation_id"] == created["conversation"]["id"]
    assert result["notification"]["target_type"] == "conversation"
    assert result["evidence_count"] >= 1
    push_turns = store.list_turns(created["conversation"]["id"], user_id="schedule_user")
    assert any(turn["payload"].get("type") == "scheduled_push_result" for turn in push_turns)


def test_schedule_chat_runs_cc_project_skill_before_persisting(services):
    registry, store, search = services
    reports = ReportGenerationService(store, search)
    runtime = FakeScenarioCCRuntime()
    tasks = ScheduledTaskService(store, reports, llm_client=FakeDisabledLLM(), cc_runtime=runtime)
    chat = NewsChatService(
        store,
        search,
        scheduled_tasks=tasks,
        cc_runtime=runtime,
        skill_registry=build_default_registry(),
        services={"tasks": tasks, "reports": reports, "registry": registry, "cc_runtime": runtime, "search": search},
    )

    response = asyncio.run(
        chat.chat(
            "schedule_cc_skill_conv",
            "/schedule 每周一上午8点汇总周末AI芯片热点并生成专题报告",
            user_id="schedule_cc_skill_user",
            allow_web_search=True,
        )
    )

    assert response.context_relation == "scheduled_push_created"
    assert response.skill_result["command"] == "/schedule"
    assert runtime.skill_names[-1] == [SCHEDULED_NEWS_TASK_SKILL_NAME]
    assert response.skill_result["data"]["task"]["schedule"] == "0 8 * * 1"
    assert any(item["stage"] == "定时任务 Skill" for item in response.research_trace)


def test_report_brief_and_sources_use_dedicated_cc_project_skills(services):
    registry, store, search = services
    reports = ReportGenerationService(store, search)
    runtime = FakeScenarioCCRuntime()
    chat = NewsChatService(
        store,
        search,
        cc_runtime=runtime,
        skill_registry=build_default_registry(),
        services={"reports": reports, "registry": registry, "cc_runtime": runtime, "search": search},
    )

    report = asyncio.run(chat.chat("scenario_skills_conv", "/report AI Agent", user_id="scenario_user", allow_web_search=True))
    brief = asyncio.run(chat.chat("scenario_skills_conv", "/brief AI Agent", user_id="scenario_user", allow_web_search=True))
    sources = asyncio.run(chat.chat("scenario_skills_conv", "/sources tech", user_id="scenario_user", allow_web_search=True))

    assert report.skill_result["data"]["agent_source"] == "cc_runtime"
    assert brief.skill_result["data"]["agent_source"] == "cc_runtime"
    assert sources.skill_result["data"]["agent_source"] == "cc_runtime"
    assert [NEWS_TOPIC_REPORT_SKILL_NAME] in runtime.skill_names
    assert [NEWS_DAILY_BRIEF_SKILL_NAME] in runtime.skill_names
    assert [NEWS_SOURCE_AUDIT_SKILL_NAME] in runtime.skill_names


def test_parse_weekly_schedule_command():
    parsed = parse_schedule_command("/schedule 每周一上午8点给我关于世界杯的新闻专题")
    assert parsed["schedule"] == "0 8 * * 1"
    assert parsed["topic"] == "世界杯"


def test_schedule_command_uses_llm_standard_api_params(services):
    _, store, search = services
    reports = ReportGenerationService(store, search)
    tasks = ScheduledTaskService(store, reports, llm_client=FakeScheduleLLM())

    created = asyncio.run(
        tasks.create_from_schedule_message(
            "llm_schedule_user",
            "/schedule 每天上午9点收集关于俄乌冲突的新闻，并总结成一个简短军事早报发给我",
        )
    )

    task = store.get_task(created["task"]["id"])
    assert task["user_id"] == "llm_schedule_user"
    assert task["task_type"] == "scheduled_push"
    assert task["schedule_cron"] == "0 9 * * *"
    assert task["topics"] == ["俄乌冲突"]
    assert task["category_scope"] == ["military"]
    assert task["raw_task_description"].startswith("/schedule 每天上午9点")
    assert task["parsed_workflow"]["search_queries"] == ["俄乌冲突 最新", "俄乌冲突 军事动态"]
    assert task["output_style"] == "简短军事早报"
    assert task["parsed_workflow"]["report_style"]["name"] == "简短军事早报"


class FakeScheduleLLM:
    configured = True

    async def structured(self, messages, schema_name, schema, model_key=None):
        return {
            "user_id": "wrong_user_should_be_ignored",
            "task_type": "scheduled_push",
            "schedule": "0 9 * * *",
            "topics": ["俄乌冲突"],
            "category_scope": "military",
            "source_scope": "",
            "output_style": "军事早报",
            "delivery_channel": "in_app",
            "raw_task_description": "/schedule 每天上午9点收集关于俄乌冲突的新闻，按军事早报发给我",
            "parsed_workflow": {
                "intent_summary": "每天收集俄乌冲突相关新闻并生成军事早报。",
                "search_queries": ["俄乌冲突 最新", "俄乌冲突 军事动态"],
                "category_scope": "military",
                "source_scope": "",
                "fetch_strategy": {"mode": "search_then_fetch", "max_results": 16, "fetch_articles": 8, "max_sources": 4, "time_range_days": 3},
                "report_style": {"name": "军事早报", "tone": "简洁", "sections": ["导语", "核心事件", "影响", "来源"], "length": "600字"},
                "delivery": {"target": "Scheduled Push", "channel": "in_app"},
            },
        }


class FakeTopicSummaryLLM:
    configured = True

    async def structured(self, messages, schema_name, schema, model_key=None):
        return {
            "title": "新能源汽车价格战专题",
            "lead": "多家车企调整价格与权益，市场关注库存、利润和消费者观望。",
            "sections": [
                {
                    "title": "核心事件",
                    "body": "价格和权益调整成为近期主线。",
                    "bullets": ["车企调整主力车型价格", "经销商库存和利润承压"],
                    "evidence_indices": [1],
                }
            ],
            "timeline": [
                {
                    "date": "2026-07-14",
                    "title": "价格战进入新阶段",
                    "summary": "车企调整价格和权益。",
                    "stage": "latest",
                    "actors": ["车企"],
                    "evidence_indices": [1],
                }
            ],
            "graph": {
                "nodes": [
                    {"id": "topic", "label": "新能源汽车价格战", "type": "topic", "description": "专题中心", "evidence_indices": [1]},
                    {"id": "car_companies", "label": "车企", "type": "organization", "description": "调价主体", "evidence_indices": [1]},
                ],
                "edges": [
                    {"source": "topic", "target": "car_companies", "label": "影响", "description": "价格战影响车企利润。", "evidence_indices": [1]}
                ],
            },
            "analysis": ["价格竞争短期利好消费者，但会压缩渠道利润。"],
            "uncertainty": ["需要更多销量和库存数据验证。"],
            "markdown": "",
            "confidence": 0.72,
        }


class FakeArticleIndex:
    configured = True

    async def search(self, query, category_scope=None, source_scope=None, limit=20):
        return [
            {
                "id": "art_es_seed",
                "source_id": "people_politics",
                "title": "国际局势影响农作物出口",
                "url": "https://example.local/es",
                "summary": "粮食安全和农作物价格受到关注。",
                "category": "politics",
                "published_at": None,
            }
        ][:limit]


class FakeExternalProvider(ExternalSearchProvider):
    configured = True

    def __init__(self):
        self.calls = 0

    async def search(self, query, domains, limit):
        self.calls += 1
        return [
            RawSearchResult(
                source_id="weather.example",
                title="上海实时天气",
                url="https://weather.example/shanghai",
                snippet="上海实时天气更新。",
            )
        ][:limit]


class SemanticExternalProvider(ExternalSearchProvider):
    configured = True

    async def search(self, query, domains, limit):
        return [
            RawSearchResult(
                source_id="motorcycle.example",
                title="全球主要摩托车制造商与品牌",
                url="https://motorcycle.example/manufacturers",
                snippet="介绍本田、雅马哈、川崎、宝马等摩托车制造商。",
            )
        ][:limit]


class FakeNativeIngestion:
    def __init__(self):
        self.calls = 0

    async def ingest(self, **kwargs):
        self.calls += 1
        return {
            "discovered_count": 0,
            "fetched_count": 0,
            "indexed_count": 0,
            "mysql_ready": False,
            "elasticsearch_configured": False,
        }


class FakeDisabledLLM:
    configured = False


class FakeSearchPlannerLLM:
    configured = True

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, model_key=None):
        self.calls += 1
        return (
            "```json\n"
            '{"query":"目标对象 影响 背景事件 近期变化",'
            '"primary_subject":"目标对象受到的影响",'
            '"required_terms":["目标对象","对象变化"],'
            '"keywords":["目标对象","影响","变化"]}'
            "\n```"
        )


class FakeAnswerLLM:
    configured = True

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, model_key=None):
        self.calls += 1
        return "这段回答不应该出现。"


class EmptySearchService:
    external_configured = False

    async def search(self, query, category_scope, source_scope, time_range, max_results=20, include_remote=False):
        return []

    async def search_external(self, query, category_scope, source_scope, max_results=8):
        return []


class CaptureQuerySearchService:
    external_configured = False

    def __init__(self):
        self.queries = []

    async def search(self, query, category_scope, source_scope, time_range, max_results=20, include_remote=False):
        self.queries.append(query)
        return []

    async def search_external(self, query, category_scope, source_scope, max_results=8):
        return []


class EnoughLocalWithExternalSearchService:
    external_configured = True

    def __init__(self):
        self.external_calls = []

    async def search(self, query, category_scope, source_scope, time_range, max_results=20, include_remote=False):
        return [
            SearchResult(
                source_id="local",
                title=f"{query} 本地报道 {index}",
                url=f"https://example.com/local-{index}",
                summary=f"{query} 本地摘要 {index}",
                category=(category_scope or ["tech"])[0],
                published_at=datetime.now(timezone.utc),
                score=1.0,
                origin="local",
            )
            for index in range(7)
        ]

    async def search_external(self, query, category_scope, source_scope, max_results=8):
        self.external_calls.append(
            {
                "query": query,
                "category_scope": category_scope,
                "source_scope": source_scope,
                "max_results": max_results,
            }
        )
        return [
            SearchResult(
                source_id="external",
                title=f"{query} 联网报道",
                url="https://example.com/external",
                summary=f"{query} 联网摘要",
                category=(category_scope or ["tech"])[0],
                published_at=datetime.now(timezone.utc),
                score=1.0,
                origin="external",
            )
        ]


class FakeRelatedMessage:
    content = (
        '{"queries":['
        '{"query":"AI Agent 最新进展","relation_type":"latest","reason":"查看近期变化"},'
        '{"query":"AI Agent 产业影响","relation_type":"impact","reason":"查看影响面"}'
        "]}"
    )


class FakeRelatedLocalAgent:
    def __init__(self):
        self.calls = 0

    async def chat(self, payload):
        self.calls += 1
        return type("FakeRelatedResponse", (), {"status": "ok", "message": FakeRelatedMessage()})()


class FakeContextRewriteLocalAgent:
    config = type("FakeLocalAgentConfig", (), {"enabled": True})()

    def __init__(self, content):
        self.content = content
        self.calls = 0
        self.last_payload = None

    async def chat(self, payload):
        self.calls += 1
        self.last_payload = payload
        message = type("FakeContextRewriteMessage", (), {"content": self.content})()
        return type("FakeContextRewriteResponse", (), {"status": "ok", "message": message})()


class FakeBriefMessage:
    content = (
        '{"headline":"AI 今日简报","summary":"AI Agent 工具竞争升温。",'
        '"top_stories":[{"title":"AI Agent 产品更新","summary":"开发工具密集发布。","why_it_matters":"影响企业集成节奏。","source_index":1}],'
        '"why_it_matters":["开发工具竞争加速"],'
        '"impact":["企业集成和上下文管理会成为重点"],'
        '"watch_next":["继续观察工具调用能力"],'
        '"uncertainty":"仅基于本地证据。",'
        '"personalization_reason":"匹配用户关注 AI。"}'
    )


class FakeBriefLocalAgent:
    def __init__(self):
        self.calls = 0

    async def chat(self, payload):
        self.calls += 1
        return type("FakeBriefResponse", (), {"status": "ok", "message": FakeBriefMessage()})()


class FakeFactCheckMessage:
    content = (
        '{"verdict":"supported","confidence":0.82,'
        '"summary":"证据支持该说法。",'
        '"supporting_evidence":[1],'
        '"contradicting_evidence":[],'
        '"missing_evidence":["更多原始公告"],'
        '"source_notes":["以本地证据为准"],'
        '"next_checks":["查原始来源"]}'
    )


class FakeFactCheckLocalAgent:
    def __init__(self):
        self.calls = 0

    async def chat(self, payload):
        self.calls += 1
        return type("FakeFactCheckResponse", (), {"status": "ok", "message": FakeFactCheckMessage()})()


class FakeFactCheckLLM:
    configured = True

    def __init__(self):
        self.calls = 0

    async def structured(self, messages, schema_name, schema, model_key=None):
        self.calls += 1
        assert schema_name == "factcheck_verdict"
        assert any("evidence" in message["content"] for message in messages if message["role"] == "user")
        assert any("不可信" in message["content"] for message in messages if message["role"] == "system")
        return {
            "verdict": "supported",
            "confidence": 0.76,
            "summary": "本地和联网证据共同支持该说法。",
            "supporting_evidence": [999, 1],
            "contradicting_evidence": [],
            "missing_evidence": ["原始公告"],
            "source_notes": ["由结构化大模型汇总"],
            "next_checks": ["继续核对原始公告"],
        }


class FakeFailingLocalAgent:
    async def chat(self, payload):
        raise RuntimeError("agent unavailable")


class FakeRelatedSearchService:
    def __init__(self):
        self.queries = []

    async def search(self, query, category_scope, source_scope, time_range, max_results=20, include_remote=False):
        self.queries.append(query)
        return [
            SearchResult(
                source_id="test",
                title=f"{query} 报道",
                url=f"https://example.com/{len(self.queries)}",
                summary=f"{query} 的摘要",
                category=(category_scope or ["tech"])[0],
                published_at=datetime.now(timezone.utc),
                score=1.0,
                origin="local",
            )
        ]


class RecordingSearchService:
    def __init__(self):
        self.calls = []
        self.external_calls = []
        self.external_configured = True

    async def search(self, query, category_scope, source_scope, time_range, max_results=20, include_remote=False):
        self.calls.append(
            {
                "query": query,
                "category_scope": category_scope,
                "source_scope": source_scope,
                "time_range": time_range,
                "include_remote": include_remote,
            }
        )
        return [
            SearchResult(
                source_id="test",
                title=f"{query} 报道",
                url="https://example.com/unrelated",
                summary=f"{query} 的摘要",
                category=(category_scope or ["all"])[0],
                published_at=datetime.now(timezone.utc),
                score=1.0,
                origin="local",
            )
        ]

    async def search_external(self, query, category_scope, source_scope, max_results=8):
        self.external_calls.append(
            {
                "query": query,
                "category_scope": category_scope,
                "source_scope": source_scope,
                "max_results": max_results,
            }
        )
        return [
            SearchResult(
                source_id="web.example",
                title=f"{query} 联网核查",
                url="https://web.example/factcheck",
                summary=f"{query} 的联网证据摘要",
                category=(category_scope or ["all"])[0],
                published_at=datetime.now(timezone.utc),
                score=0.8,
                origin="external",
            )
        ]


class FakeCCRuntime:
    configured = True

    def __init__(self):
        self.calls = 0
        self.logical_model_key = None
        self.skill_names = None
        self.last_kwargs = None

    async def run(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        assert kwargs["allow_web_search"] is True
        self.logical_model_key = kwargs["logical_model_key"]
        self.skill_names = kwargs.get("skill_names")
        query = kwargs.get("query") or "测试主题"
        return CCRuntimeResult(
            answer="## Runtime 汇总\n\n证据支持这项变化。",
            results=[
                SearchResult(
                    source_id="runtime.local",
                    title=f"{query} 最新变化",
                    url="https://runtime.example/ai-agent",
                    summary=f"{query} 的证据摘要。",
                    category="tech",
                    published_at=datetime.now(timezone.utc),
                    score=1.0,
                    origin="local",
                )
            ],
            queries=[{"query": query, "origin": "local", "result_count": 1}],
            trace=[
                {
                    "stage": "本地新闻引擎",
                    "status": "completed",
                    "message": "检索返回 1 条候选。",
                    "count": 1,
                }
            ],
        )


class FakeScenarioCCRuntime:
    configured = True

    def __init__(self):
        self.skill_names = []

    async def run(self, **kwargs):
        selected = kwargs.get("skill_names") or []
        self.skill_names.append(selected)
        if selected == [SCHEDULED_NEWS_TASK_SKILL_NAME]:
            answer = json.dumps(
                {
                    "user_id": "wrong-owner",
                    "task_type": "scheduled_push",
                    "schedule": "0 8 * * 1",
                    "topics": ["AI芯片热点"],
                    "category_scope": ["tech"],
                    "source_scope": [],
                    "output_style": "专题报告",
                    "delivery_channel": "in_app",
                    "raw_task_description": "wrong-description",
                    "parsed_workflow": {
                        "intent_summary": "汇总周末AI芯片热点",
                        "search_queries": ["AI芯片 周末 热点"],
                        "category_scope": ["tech"],
                        "source_scope": [],
                        "fetch_strategy": {"max_results": 12, "fetch_articles": 6, "max_sources": 3},
                        "report_style": {"sections": ["摘要", "事件", "来源"]},
                        "delivery": {"channel": "in_app"},
                    },
                },
                ensure_ascii=False,
            )
            results = []
        else:
            answer = "## Agent 场景结果\n\n已根据证据完成结构化输出。\n\n### 来源\n\n- [测试来源](https://example.com/news)"
            results = [
                SearchResult(
                    source_id="test",
                    title="测试来源",
                    url="https://example.com/news",
                    summary="用于验证场景 Skill 路由。",
                    category="tech",
                    published_at=datetime.now(timezone.utc),
                    origin="local",
                )
            ]
        trace = {"stage": "Agent 主控", "status": "completed", "message": "场景工作流执行完成。"}
        if kwargs.get("on_trace"):
            await kwargs["on_trace"](trace)
        return CCRuntimeResult(answer=answer, results=results, trace=[trace], queries=[])


class FakeRelatedCCRuntime:
    configured = True

    def __init__(self):
        self.kwargs = None

    async def run(self, **kwargs):
        self.kwargs = kwargs
        trace = {
            "stage": "外部搜索工具",
            "status": "completed",
            "message": "已完成 2 次外部证据检索。",
            "count": 2,
        }
        if kwargs.get("on_trace"):
            await kwargs["on_trace"](trace)
        return CCRuntimeResult(
            answer=(
                "结合刚才的 TI 话题，你这里说的 Ame 是 Dota 2 职业选手。\n\n"
                "### 他与当前事件的关系\n\nAme 是中国赛区受到关注的 carry 选手。\n\n"
                "### 来源\n\n- [赛事资料](https://example.com/ame-ti)"
            ),
            results=[
                SearchResult(
                    source_id="gamersky",
                    title="沈腾新片 _ GamerSky.com",
                    url="https://example.com/movie",
                    summary="上海暑期档电影资讯",
                    category="entertainment",
                    origin="local",
                ),
                SearchResult(
                    source_id="esports",
                    title="Dota 2 选手 Ame 备战 TI",
                    url="https://example.com/ame-ti",
                    summary="Ame 与中国战队参加上海赛事的动态。",
                    category="game",
                    origin="local",
                ),
            ],
            queries=[
                {"query": "Dota 2 Ame TI 上海", "origin": "local", "result_count": 1},
                {"query": "Dota 2 Ame current roster", "origin": "web", "result_count": 3},
            ],
            trace=[trace],
            provider_metadata={"builtin_web_calls": 2},
        )


class FakeProjectSkillCCRuntime:
    configured = True

    def __init__(self):
        self.skill_names = None

    async def run(self, **kwargs):
        self.skill_names = kwargs.get("skill_names")
        trace = {
            "stage": "本地新闻引擎",
            "status": "completed",
            "message": "检索返回 1 条候选。",
            "count": 1,
        }
        if kwargs.get("on_trace"):
            await kwargs["on_trace"](trace)
        result = SearchResult(
            source_id="runtime.local",
            title="AI Agent 产品更新",
            url="https://runtime.example/factcheck",
            summary="产品更新已经发布。",
            category="tech",
            published_at=datetime.now(timezone.utc),
            score=1.0,
            origin="local",
        )
        if self.skill_names == ["news-fact-check"]:
            answer = json.dumps(
                {
                    "verdict": "supported",
                    "confidence": 0.86,
                    "summary": "直接报道支持该说法。",
                    "supporting_evidence": [
                        {"url": "https://runtime.example/factcheck", "title": "AI Agent 产品更新"}
                    ],
                    "contradicting_evidence": [],
                    "missing_evidence": [],
                    "source_notes": ["当前保留一条直接报道。"],
                    "next_checks": ["继续观察后续公告。"],
                },
                ensure_ascii=False,
            )
        else:
            answer = (
                "## 事件图谱\n\n```mermaid\nflowchart LR\n"
                '  event["AI Agent 产品更新"] --> impact["开发工具竞争"]\n'
                "```\n\n## 关键解读\n\n- 产品更新推动竞争。\n\n"
                "## 证据来源\n\n- [AI Agent 产品更新](https://runtime.example/factcheck)\n\n"
                "## 不确定性\n\n- 后续影响仍待观察。"
            )
        return CCRuntimeResult(
            answer=answer,
            results=[result],
            queries=[{"query": kwargs["query"], "origin": "local", "result_count": 1}],
            trace=[trace],
        )


class FakeFailingCCRuntime:
    configured = True

    async def run(self, **kwargs):
        raise RuntimeError("runtime unavailable")


class FakeClaudeSDKClient:
    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def query(self, prompt):
        assert "总结 AI Agent 变化" in prompt

    async def receive_response(self):
        text_block = type("TextBlock", (), {"text": "中间文本"})()
        yield type("AssistantMessage", (), {"content": [text_block]})()
        yield type(
            "ResultMessage",
            (),
            {
                "is_error": False,
                "result": "## SDK 最终回答",
                "session_id": "sdk-session",
                "num_turns": 2,
                "duration_ms": 35,
                "total_cost_usd": 0.01,
            },
        )()


class FakeWebRequiredClaudeSDKClient:
    def __init__(self, options):
        self.options = options
        self.query_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def query(self, prompt):
        self.query_count += 1
        if self.query_count == 1:
            assert "总结 AI Agent 变化" in prompt
        else:
            assert "必须先调用 CC 自带的 WebSearch" in prompt

    async def receive_response(self):
        content = []
        if self.query_count == 2:
            content.append(type("ToolUseBlock", (), {"name": "WebSearch"})())
        content.append(type("TextBlock", (), {"text": "中间文本"})())
        yield type("AssistantMessage", (), {"content": content})()
        final = "## 首轮回答" if self.query_count == 1 else "## 已核对外部信息的最终回答"
        yield type(
            "ResultMessage",
            (),
            {
                "is_error": False,
                "result": final,
                "session_id": "sdk-web-session",
                "num_turns": self.query_count,
                "duration_ms": 35,
                "total_cost_usd": 0.01,
            },
        )()


class FakeLinkFetcher:
    def __init__(self):
        self.urls = []

    async def list_links(self, source_id, section_key, url, limit=30, allowed_domains=None):
        self.urls.append(url)
        return [
            RawArticleLink(
                source_id=source_id,
                section_key=section_key,
                title="机车赛事更新",
                url="https://bbs.hupu.com/639652293.html",
            )
        ][:limit]

    async def fetch_article(self, source_id, url):
        return RawArticle(
            source_id=source_id,
            url=url,
            title="机车赛事更新",
            summary="机车赛事继续受到关注。",
            content="机车赛事继续受到关注，车队成绩、商业合作和舆论讨论同步升温。",
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
