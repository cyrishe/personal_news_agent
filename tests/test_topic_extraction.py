from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

from personal_news_agent.core.models import NormalizedArticle
from personal_news_agent.core.text import content_hash
from personal_news_agent.services.store import NewsStore
from personal_news_agent.services.topic_extraction import TopicExtractionService
from personal_news_agent.services.topic_extraction import TopicExtraction


class FakeTopicLLM:
    configured = True

    def __init__(self, invalid_id: bool = False):
        self.invalid_id = invalid_id
        self.calls = []

    async def structured(self, messages, schema_name, schema, model_key=None):
        self.calls.append(messages)
        import json

        payload = json.loads(messages[-1]["content"])
        recent = payload["recent_topics"]
        existing = "invented_topic" if self.invalid_id else (recent[0]["topic_id"] if recent else None)
        return {
            "category": payload["article"]["bound_category"],
            "subject": "国家足球队",
            "topic_name": "世界杯预选赛",
            "existing_topic_id": existing,
            "event_summary": "球队公布最新阵容，并确认下一场世界杯预选赛的比赛安排。",
            "keywords": ["世界杯", "阵容"],
            "confidence": 0.92,
        }


class UnconfiguredLLM:
    configured = False


class UnauthorizedTopicLLM:
    configured = True

    def __init__(self):
        self.calls = 0

    async def structured(self, messages, schema_name, schema, model_key=None):
        self.calls += 1
        raise RuntimeError("401 Authorization Required")


def _article(article_id: str, title: str) -> NormalizedArticle:
    body = f"{title}。球队公布最新阵容，下一场比赛将在本周进行，这是用于主题抽取测试的正文。"
    return NormalizedArticle(
        id=article_id, source_id="test", section_key="sports", url=f"https://example.com/{article_id}.html",
        title=title, summary="", content=body, category="sports", published_at=datetime.now(timezone.utc),
        fetched_at=datetime.now(timezone.utc), source_priority=1, keywords=[], entities=[], content_hash=content_hash(body),
    )


def test_topic_extraction_creates_then_merges_recent_topic(tmp_path):
    store = NewsStore(tmp_path / "news.db"); store.init()
    store.save_article(_article("a1", "国家队公布世界杯预选赛阵容"))
    store.save_article(_article("a2", "世界杯预选赛下一场赛程确认"))
    llm = FakeTopicLLM()
    service = TopicExtractionService(store, llm=llm)

    result = asyncio.run(service.process_pending(limit=2))

    assert result["processed"] == 2
    assert result["items"][0]["merged"] is False
    assert result["items"][1]["merged"] is True
    assert result["items"][0]["topic_id"] == result["items"][1]["topic_id"]
    assert result["items"][0]["subject"] == "国家足球队"
    assert len(store.list_recent_news_topics("sports")) == 1
    assert "recent_topics" in llm.calls[1][-1]["content"]
    payload = json.loads(llm.calls[0][-1]["content"])
    assert "content" not in payload["article"]
    assert len(payload["article"]["content_excerpt"]) <= 4000
    with store.connect() as conn:
        subjects = [row[0] for row in conn.execute("SELECT subject FROM news_topic_articles ORDER BY article_id").fetchall()]
    assert subjects == ["国家足球队", "国家足球队"]


def test_topic_extraction_rejects_invented_topic_id(tmp_path):
    store = NewsStore(tmp_path / "news.db"); store.init()
    store.save_article(_article("a1", "国家队公布世界杯预选赛阵容"))
    result = asyncio.run(TopicExtractionService(store, llm=FakeTopicLLM(invalid_id=True)).process_pending(limit=1))
    assert result["processed"] == 0
    assert "outside recent_topics" in result["errors"][0]["error"]
    assert len(store.list_unprocessed_topic_articles()) == 1


def test_topic_extraction_skips_without_configured_llm(tmp_path):
    store = NewsStore(tmp_path / "news.db"); store.init()
    result = asyncio.run(TopicExtractionService(store, llm=UnconfiguredLLM()).process_pending())
    assert result == {"status": "skipped", "reason": "llm_not_configured", "processed": 0, "errors": []}


def test_topic_extraction_auth_failure_stops_batch_and_enters_cooldown(tmp_path):
    store = NewsStore(tmp_path / "news.db"); store.init()
    store.save_article(_article("a1", "国家队公布世界杯预选赛阵容"))
    store.save_article(_article("a2", "世界杯预选赛下一场赛程确认"))
    llm = UnauthorizedTopicLLM()
    service = TopicExtractionService(store, llm=llm)

    first = asyncio.run(service.process_pending(limit=20))
    second = asyncio.run(service.process_pending(limit=20))

    assert llm.calls == 1
    assert len(first["errors"]) == 1
    assert second["status"] == "skipped"
    assert second["reason"] == "provider_cooldown"


def test_pending_topic_articles_prioritize_latest_and_ignore_future_dates(tmp_path):
    store = NewsStore(tmp_path / "news.db"); store.init()
    now = datetime.now(timezone.utc)
    old = _article("old", "较早报道")
    latest = _article("latest", "最新报道")
    future = _article("future", "错误的未来日期报道")
    store.save_article(replace(old, published_at=now - timedelta(days=2)))
    store.save_article(replace(latest, published_at=now - timedelta(minutes=5)))
    store.save_article(replace(future, published_at=now + timedelta(days=30)))

    pending = store.list_unprocessed_topic_articles(limit=10)

    assert [item["id"] for item in pending] == ["latest", "old"]


def test_topic_extraction_schema_requires_every_output_field():
    schema = TopicExtraction.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
