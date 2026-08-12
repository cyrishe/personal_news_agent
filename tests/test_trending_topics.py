from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json

from personal_news_agent.core.models import NormalizedArticle
from personal_news_agent.core.text import content_hash
from personal_news_agent.services.store import NewsStore
from personal_news_agent.services.cc_runtime import CCRuntimeResult
from personal_news_agent.services.trending_topics import (
    TrendingTopicBatch,
    TrendingTopicService,
    _balanced_title_rows,
    _normalize_batch_payload,
)


class FakeTrendingLLM:
    configured = True

    def __init__(self, topics: list[dict]):
        self.topics = topics
        self.calls: list[list[dict]] = []

    async def structured(self, messages, schema_name, schema, model_key=None):
        self.calls.append(messages)
        assert schema_name == "trending_topic_batch"
        assert schema["additionalProperties"] is False
        return {"topics": self.topics}


class DisabledTrendingLLM:
    configured = False


class FakeTrendingCCRuntime:
    configured = True

    def __init__(self):
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        return CCRuntimeResult(
            answer=json.dumps(
                {
                    "topics": [
                        {
                            "category": "sports",
                            "title": "国家队公布世界杯预选赛阵容",
                            "summary": "多家门户持续报道国家队公布世界杯预选赛参赛阵容。",
                            "keywords": ["国家队", "世界杯预选赛", "阵容"],
                            "article_ids": ["sport_1", "sport_2", "sport_3"],
                            "confidence": 0.95,
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            results=[],
        )


def _article(article_id: str, source_id: str, title: str, category: str, age_hours: float) -> NormalizedArticle:
    now = datetime.now(timezone.utc)
    body = f"{title}。这是正文，但热点归并模型只应接收标题和系统统计所需字段。"
    return NormalizedArticle(
        id=article_id,
        source_id=source_id,
        section_key=category,
        url=f"https://{source_id}.example.com/{article_id}.html",
        title=title,
        summary=f"{title}摘要",
        content=body,
        category=category,
        published_at=now - timedelta(hours=age_hours),
        fetched_at=now - timedelta(hours=age_hours),
        source_priority=1,
        keywords=[],
        entities=[],
        content_hash=content_hash(body),
    )


def _store(tmp_path) -> NewsStore:
    store = NewsStore(tmp_path / "trending.db")
    store.init()
    store.save_profile(
        {
            "user_id": "sports_user",
            "interests": ["世界杯", "国家队"],
            "negative_interests": [],
            "preferred_categories": ["sports"],
            "preferred_sources": [],
            "output_style": "concise",
        }
    )
    return store


def test_trending_topics_use_title_window_and_system_heat_metrics(tmp_path):
    store = _store(tmp_path)
    for article in (
        _article("sport_1", "sina", "国家队公布世界杯预选赛最新阵容", "sports", 2.5),
        _article("sport_2", "sohu", "世界杯预选赛：国家队新阵容公布", "sports", 1.5),
        _article("sport_3", "cctv", "国家队确认世界杯预选赛参赛阵容", "sports", 0.5),
        _article("tech_1", "ithome", "某公司发布新款笔记本电脑", "tech", 0.4),
    ):
        store.save_article(article)

    llm = FakeTrendingLLM(
        [
            {
                "category": "sports",
                "title": "国家队公布世界杯预选赛阵容",
                "summary": "多家门户持续报道国家队公布世界杯预选赛参赛阵容。",
                "keywords": ["国家队", "世界杯预选赛", "阵容"],
                "article_ids": ["sport_1", "sport_2", "sport_3"],
                "confidence": 0.95,
            },
            {
                "category": "tech",
                "title": "某公司发布新款笔记本电脑",
                "summary": "一家门户报道某公司发布新款笔记本电脑。",
                "keywords": ["笔记本电脑"],
                "article_ids": ["tech_1"],
                "confidence": 0.8,
            },
        ]
    )
    service = TrendingTopicService(store, llm=llm)
    result = asyncio.run(service.recommend("sports_user", limit=2, window_hours=24, refresh_window_hours=6))

    assert result["generation_source"] == "llm"
    assert result["items"][0]["category"] == "sports"
    assert result["items"][0]["source_count"] == 3
    assert result["items"][0]["article_count"] == 3
    assert result["items"][0]["recent_update_count"] == 3
    assert result["items"][0]["evidence_level"] == "hot"
    assert result["items"][1]["evidence_level"] == "lead"
    assert result["items"][0]["hot_score"] > result["items"][1]["hot_score"]
    assert "匹配关注" in result["items"][0]["recommend_reason"]
    payload = json.loads(llm.calls[0][-1]["content"])
    assert {item["article_id"] for item in payload["articles"]} == {"sport_1", "sport_2", "sport_3", "tech_1"}
    assert all("content" not in item and "summary" not in item for item in payload["articles"])


def test_trending_topics_supplement_missing_preferred_category_with_real_article(tmp_path):
    store = _store(tmp_path)
    for article in (
        _article("economy_1", "finance", "多家机构发布最新经济数据", "economy", 0.4),
        _article("sports_1", "sports", "世界杯预选赛国家队完成赛前训练", "sports", 0.8),
    ):
        store.save_article(article)
    llm = FakeTrendingLLM(
        [
            {
                "category": "economy",
                "title": "多家机构发布最新经济数据",
                "summary": "多家机构发布最新经济数据并引发市场关注。",
                "keywords": ["经济数据"],
                "article_ids": ["economy_1"],
                "confidence": 0.9,
            }
        ]
    )

    result = asyncio.run(TrendingTopicService(store, llm=llm).recommend("sports_user", limit=2))

    assert result["generation_source"] == "llm"
    assert result["items"][0]["category"] == "sports"
    assert result["items"][0]["article_ids"] == ["sports_1"]
    assert result["items"][0]["generation_source"] == "recent_article"
    assert "匹配关注" in result["items"][0]["recommend_reason"]


def test_ui_cache_preference_returns_fast_fallback_then_uses_warmed_llm_result(tmp_path):
    store = _store(tmp_path)
    store.save_article(_article("sport_fast", "sports", "国家队公布世界杯预选赛阵容", "sports", 0.2))
    llm = FakeTrendingLLM(
        [
            {
                "category": "sports",
                "title": "国家队公布世界杯预选赛阵容",
                "summary": "国家队公布世界杯预选赛阵容并引发持续关注。",
                "keywords": ["国家队", "世界杯预选赛"],
                "article_ids": ["sport_fast"],
                "confidence": 0.9,
            }
        ]
    )
    service = TrendingTopicService(store, llm=llm, cache_minutes=3)

    first = asyncio.run(service.recommend("sports_user", prefer_cached=True))
    assert first["generation_source"] == "fallback"
    assert llm.calls == []

    warmed = asyncio.run(service.recommend("sports_user"))
    assert warmed["generation_source"] == "llm"
    assert len(llm.calls) == 1

    cached = asyncio.run(service.recommend("sports_user", prefer_cached=True))
    assert cached["generation_source"] == "llm"
    assert len(llm.calls) == 1


def test_trending_fallback_keeps_a_real_candidate_for_preferred_category(tmp_path):
    store = _store(tmp_path)
    for index in range(24):
        store.save_article(
            _article(
                f"economy_{index}",
                "finance",
                f"机构发布第{index + 1}项经济运行观察数据",
                "economy",
                0.2 + index * 0.01,
            )
        )
    store.save_article(_article("sports_only", "sports", "世界杯国家队今日完成公开训练", "sports", 2.0))

    result = asyncio.run(
        TrendingTopicService(store, llm=DisabledTrendingLLM()).recommend("sports_user", limit=3)
    )

    assert result["generation_source"] == "fallback"
    assert result["items"][0]["category"] == "sports"
    assert result["items"][0]["article_ids"] == ["sports_only"]


def test_trending_topics_reject_invented_article_ids_and_fall_back(tmp_path):
    store = _store(tmp_path)
    store.save_article(_article("real_1", "sina", "真实的近期体育事件", "sports", 1))
    llm = FakeTrendingLLM(
        [
            {
                "category": "sports",
                "title": "模型编造的热点",
                "summary": "这条结果引用了输入中不存在的文章，因此不能采用。",
                "keywords": ["编造"],
                "article_ids": ["invented_article"],
                "confidence": 0.99,
            }
        ]
    )

    result = asyncio.run(TrendingTopicService(store, llm=llm).recommend("sports_user", limit=3))

    assert result["generation_source"] == "fallback"
    assert result["items"]
    assert result["items"][0]["article_ids"] == ["real_1"]
    assert all("invented_article" not in item["article_ids"] for item in result["items"])


def test_trending_topics_prefer_cc_runtime_for_title_clustering(tmp_path):
    store = _store(tmp_path)
    for article in (
        _article("sport_1", "sina", "国家队公布世界杯预选赛最新阵容", "sports", 2.5),
        _article("sport_2", "sohu", "世界杯预选赛：国家队新阵容公布", "sports", 1.5),
        _article("sport_3", "cctv", "国家队确认世界杯预选赛参赛阵容", "sports", 0.5),
    ):
        store.save_article(article)
    runtime = FakeTrendingCCRuntime()

    result = asyncio.run(
        TrendingTopicService(store, llm=DisabledTrendingLLM(), cc_runtime=runtime).recommend(
            "sports_user",
            limit=2,
        )
    )

    assert result["generation_source"] == "cc_runtime"
    assert result["items"][0]["article_count"] == 3
    assert runtime.calls[0]["strict_json_output"] is True
    assert runtime.calls[0]["allow_web_search"] is False
    assert runtime.calls[0]["allow_local_search"] is False
    assert runtime.calls[0]["timeout_seconds"] == 90


def test_unchanged_title_window_reuses_model_clusters_without_new_call(tmp_path):
    store = _store(tmp_path)
    for article in (
        _article("sport_1", "sina", "国家队公布世界杯预选赛最新阵容", "sports", 2.5),
        _article("sport_2", "sohu", "世界杯预选赛：国家队新阵容公布", "sports", 1.5),
        _article("sport_3", "cctv", "国家队确认世界杯预选赛参赛阵容", "sports", 0.5),
    ):
        store.save_article(article)
    runtime = FakeTrendingCCRuntime()
    service = TrendingTopicService(store, llm=DisabledTrendingLLM(), cc_runtime=runtime, cache_minutes=1)

    first = asyncio.run(service.recommend("sports_user", limit=2))
    service._cache.clear()
    second = asyncio.run(service.recommend("sports_user", limit=2))

    assert first["generation_source"] == "cc_runtime"
    assert second["generation_source"] == "model_cache"
    assert len(runtime.calls) == 1
    assert second["items"][0]["source_count"] == 3


def test_model_title_state_survives_service_restart(tmp_path):
    store = _store(tmp_path)
    for article in (
        _article("sport_1", "sina", "国家队公布世界杯预选赛最新阵容", "sports", 2.5),
        _article("sport_2", "sohu", "世界杯预选赛：国家队新阵容公布", "sports", 1.5),
        _article("sport_3", "cctv", "国家队确认世界杯预选赛参赛阵容", "sports", 0.5),
    ):
        store.save_article(article)
    first_runtime = FakeTrendingCCRuntime()
    asyncio.run(
        TrendingTopicService(store, llm=DisabledTrendingLLM(), cc_runtime=first_runtime).recommend(
            "sports_user", limit=2
        )
    )
    second_runtime = FakeTrendingCCRuntime()

    result = asyncio.run(
        TrendingTopicService(store, llm=DisabledTrendingLLM(), cc_runtime=second_runtime).recommend(
            "sports_user", limit=2
        )
    )

    assert len(first_runtime.calls) == 1
    assert second_runtime.calls == []
    assert result["generation_source"] == "model_cache"


def test_small_title_delta_is_visible_immediately_and_waits_for_batch(tmp_path):
    store = _store(tmp_path)
    for article in (
        _article("sport_1", "sina", "国家队公布世界杯预选赛最新阵容", "sports", 2.5),
        _article("sport_2", "sohu", "世界杯预选赛：国家队新阵容公布", "sports", 1.5),
        _article("sport_3", "cctv", "国家队确认世界杯预选赛参赛阵容", "sports", 0.5),
    ):
        store.save_article(article)
    runtime = FakeTrendingCCRuntime()
    service = TrendingTopicService(store, llm=DisabledTrendingLLM(), cc_runtime=runtime, cache_minutes=1)
    asyncio.run(service.recommend("sports_user", limit=6))
    store.save_article(_article("fresh_1", "xinhua", "女足国家队公布最新集训名单", "sports", 0.1))
    service._cache.clear()

    result = asyncio.run(service.recommend("sports_user", limit=6))

    assert len(runtime.calls) == 1
    assert result["generation_source"] == "model_cache"
    fresh = next(item for item in result["items"] if item["article_ids"] == ["fresh_1"])
    assert fresh["evidence_level"] == "lead"


def test_accumulated_title_batch_triggers_one_incremental_model_call(tmp_path):
    store = _store(tmp_path)
    for article in (
        _article("sport_1", "sina", "国家队公布世界杯预选赛最新阵容", "sports", 2.5),
        _article("sport_2", "sohu", "世界杯预选赛：国家队新阵容公布", "sports", 1.5),
        _article("sport_3", "cctv", "国家队确认世界杯预选赛参赛阵容", "sports", 0.5),
    ):
        store.save_article(article)
    runtime = FakeTrendingCCRuntime()
    service = TrendingTopicService(store, llm=DisabledTrendingLLM(), cc_runtime=runtime, cache_minutes=1)
    asyncio.run(service.recommend("sports_user", limit=6))
    for index in range(20):
        store.save_article(
            _article(
                f"fresh_{index}",
                f"source_{index % 4}",
                f"第{index + 1}条近期体育标题更新",
                "sports",
                0.1,
            )
        )
    service._cache.clear()
    service._last_model_at = datetime.now(timezone.utc) - timedelta(minutes=16)

    asyncio.run(service.recommend("sports_user", limit=6))

    assert len(runtime.calls) == 2
    payload = json.loads(runtime.calls[1]["message"].split("\n", 1)[1])
    assert len(payload["articles"]) <= 80
    assert any(item["article_id"].startswith("fresh_") for item in payload["articles"])


def test_large_title_burst_inside_minimum_interval_is_batched(tmp_path):
    store = _store(tmp_path)
    for article in (
        _article("sport_1", "sina", "国家队公布世界杯预选赛最新阵容", "sports", 2.5),
        _article("sport_2", "sohu", "世界杯预选赛：国家队新阵容公布", "sports", 1.5),
        _article("sport_3", "cctv", "国家队确认世界杯预选赛参赛阵容", "sports", 0.5),
    ):
        store.save_article(article)
    runtime = FakeTrendingCCRuntime()
    service = TrendingTopicService(store, llm=DisabledTrendingLLM(), cc_runtime=runtime, cache_minutes=1)
    asyncio.run(service.recommend("sports_user", limit=6))
    for index in range(30):
        store.save_article(
            _article(f"burst_{index}", f"source_{index % 5}", f"突发更新标题{index}", "sports", 0.1)
        )
    service._cache.clear()

    result = asyncio.run(service.recommend("sports_user", limit=6))

    assert len(runtime.calls) == 1
    assert result["generation_source"] == "model_cache"


def test_cross_source_burst_can_refresh_before_regular_batch_interval(tmp_path):
    store = _store(tmp_path)
    for article in (
        _article("sport_1", "sina", "国家队公布世界杯预选赛最新阵容", "sports", 2.5),
        _article("sport_2", "sohu", "世界杯预选赛：国家队新阵容公布", "sports", 1.5),
        _article("sport_3", "cctv", "国家队确认世界杯预选赛参赛阵容", "sports", 0.5),
    ):
        store.save_article(article)
    runtime = FakeTrendingCCRuntime()
    service = TrendingTopicService(store, llm=DisabledTrendingLLM(), cc_runtime=runtime, cache_minutes=1)
    asyncio.run(service.recommend("sports_user", limit=6))
    for index in range(8):
        store.save_article(
            _article(f"cross_{index}", f"source_{index % 3}", f"跨门户突发标题{index}", "sports", 0.1)
        )
    service._cache.clear()
    service._last_model_at = datetime.now(timezone.utc) - timedelta(minutes=7)

    asyncio.run(service.recommend("sports_user", limit=6))

    assert len(runtime.calls) == 2


def test_provider_auth_failure_uses_cooldown_instead_of_repeating(tmp_path):
    class UnauthorizedRuntime:
        configured = True

        def __init__(self):
            self.calls = 0

        async def run(self, **kwargs):
            self.calls += 1
            raise RuntimeError("401 Authorization Required")

    store = _store(tmp_path)
    store.save_article(_article("sport_1", "sina", "国家队公布最新阵容", "sports", 0.2))
    runtime = UnauthorizedRuntime()
    service = TrendingTopicService(store, llm=DisabledTrendingLLM(), cc_runtime=runtime, cache_minutes=1)

    first = asyncio.run(service.recommend("sports_user", limit=2))
    service._cache.clear()
    second = asyncio.run(service.recommend("sports_user", limit=2))

    assert runtime.calls == 1
    assert first["generation_source"] == "fallback"
    assert second["generation_source"] == "fallback"
    assert service._model_retry_not_before is not None


def test_trending_topics_without_llm_returns_recent_article_fallback(tmp_path):
    store = _store(tmp_path)
    store.save_article(_article("recent_1", "cctv", "近期体育报道入口", "sports", 2))

    result = asyncio.run(TrendingTopicService(store, llm=DisabledTrendingLLM()).recommend("sports_user", limit=2))

    assert result["generation_source"] == "fallback"
    assert result["items"][0]["generation_source"] == "recent_article"
    assert result["items"][0]["topic_type"] == "recommended"


def test_single_article_candidates_are_capped_and_labeled_as_leads(tmp_path):
    store = _store(tmp_path)
    for index in range(8):
        store.save_article(
            _article(
                f"single_{index}",
                f"source_{index}",
                f"互不相关的单篇新闻线索{index}",
                "sports" if index % 2 else "tech",
                0.2 + index * 0.1,
            )
        )

    result = asyncio.run(
        TrendingTopicService(store, llm=DisabledTrendingLLM()).recommend("sports_user", limit=6)
    )

    assert len(result["items"]) == 2
    assert {item["evidence_level"] for item in result["items"]} == {"lead"}
    assert all(item["evidence_label"] == "新线索" for item in result["items"])
    assert all(item["hot_score"] < 0.25 for item in result["items"])


def test_title_window_is_balanced_across_portals_before_cc_clustering():
    rows = [
        {"article_id": f"qq_{index}", "source_id": "qq", "category": "tech", "title": "腾讯标题"}
        for index in range(20)
    ] + [
        {"article_id": f"sina_{index}", "source_id": "sina", "category": "tech", "title": "新浪标题"}
        for index in range(4)
    ]

    selected = _balanced_title_rows(rows, limit=8)

    assert [item["source_id"] for item in selected[:4]] == ["qq", "sina", "qq", "sina"]
    assert sum(item["source_id"] == "sina" for item in selected) == 4


def test_fallback_skips_social_post_titles_and_strips_source_suffixes(tmp_path):
    store = _store(tmp_path)
    store.save_article(
        _article(
            "social_1",
            "toutiao",
            "8月10日有网友发现一件非常有意思的事情，于是把前因后果全部讲了一遍，随后许多网友又发表了自己的不同看法，事情还在持续发酵中。",
            "sports",
            0.1,
        )
    )
    store.save_article(_article("clean_1", "chinanews", "国家队公布世界杯预选赛最新阵容-中新网", "sports", 1))

    result = asyncio.run(TrendingTopicService(store, llm=DisabledTrendingLLM()).recommend("sports_user", limit=3))

    assert [item["title"] for item in result["items"]] == ["国家队公布世界杯预选赛最新阵容"]


def test_llm_fallback_cache_retries_quickly(tmp_path):
    service = TrendingTopicService(_store(tmp_path), llm=DisabledTrendingLLM(), cache_minutes=15)

    assert service._cache_ttl_for({"generation_source": "fallback"}, True) == timedelta(seconds=45)
    assert service._cache_ttl_for({"generation_source": "cc_runtime"}, True) == timedelta(minutes=15)


def test_trending_topics_empty_window_is_safe(tmp_path):
    store = _store(tmp_path)

    result = asyncio.run(TrendingTopicService(store, llm=DisabledTrendingLLM()).recommend("sports_user", limit=2))

    assert result["items"] == []
    assert result["article_count"] == 0


def test_article_lists_exclude_obviously_future_dated_content(tmp_path):
    store = _store(tmp_path)
    store.save_article(_article("current_1", "cctv", "当前有效报道", "sports", 1))
    store.save_article(_article("future_1", "bad_clock", "错误的未来时间报道", "sports", -48))

    items = store.list_articles(category="sports", limit=10)

    assert [item["id"] for item in items] == ["current_1"]


def test_trending_topic_batch_accepts_observed_compatible_envelope_alias():
    raw = {
        "trending_topics": [
            {
                "category": "sports",
                "title": "世界杯预选赛阵容更新",
                "summary": "多家门户报道世界杯预选赛阵容出现新的公开信息。",
                "article_ids": ["article_1"],
            }
        ]
    }

    parsed = TrendingTopicBatch.model_validate(_normalize_batch_payload(raw))

    assert parsed.topics[0].article_ids == ["article_1"]
    assert parsed.topics[0].keywords == []
    assert parsed.topics[0].confidence == 0.65
