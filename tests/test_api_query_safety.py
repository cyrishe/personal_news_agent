from __future__ import annotations

from types import SimpleNamespace

import pytest

from personal_news_agent.services.api_query_safety import (
    ApiQuerySafetyResult,
    ApiQuerySafetyService,
    ApiQuerySafetyUnavailable,
)
from personal_news_agent.services.cc_runtime import API_QUERY_SAFETY_SKILL_NAME
from personal_news_agent.services.store import NewsStore


class FakeCCRuntime:
    configured = True

    def __init__(self, answer: str | None = None, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[dict] = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(answer=self.answer or "")


def _store(tmp_path) -> NewsStore:
    store = NewsStore(tmp_path / "safety.db")
    store.init()
    return store


@pytest.mark.asyncio
async def test_api_query_safety_passes_most_queries_without_tools(tmp_path):
    runtime = FakeCCRuntime(
        '{"decision":"pass","categories":["none"],"risk_level":"none",'
        '"reason_code":"ordinary_query","response":"must be discarded"}'
    )
    service = ApiQuerySafetyService(_store(tmp_path), runtime)

    result = await service.classify(
        "总结今天的科技新闻",
        user_id="user-1",
        conversation_id="conv-1",
        history="无",
    )

    assert result == ApiQuerySafetyResult("pass", ("none",), "none", "ordinary_query", "")
    call = runtime.calls[0]
    assert call["skill_names"] == [API_QUERY_SAFETY_SKILL_NAME]
    assert call["strict_json_output"] is True
    assert call["allow_web_search"] is False
    assert call["allow_local_search"] is False
    assert call["max_turns"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "decision", "categories"),
    [
        (
            '{"decision":"refuse","categories":["illegal_crime"],"risk_level":"high",'
            '"reason_code":"harmful_instructions","response":"我不能提供这类操作指导。"}',
            "refuse",
            ("illegal_crime",),
        ),
        (
            '{"decision":"safe_answer","categories":["sovereignty_territory"],"risk_level":"medium",'
            '"reason_code":"sovereignty_position","response":"中国的主权和领土完整不容侵犯。"}',
            "safe_answer",
            ("sovereignty_territory",),
        ),
    ],
)
async def test_api_query_safety_returns_constrained_response(tmp_path, answer, decision, categories):
    service = ApiQuerySafetyService(_store(tmp_path), FakeCCRuntime(answer))
    result = await service.classify("测试问题", user_id="user-1", conversation_id="conv-1")
    assert result.decision == decision
    assert result.categories == categories
    assert result.response


@pytest.mark.asyncio
async def test_api_query_safety_fails_closed_on_invalid_runtime_output(tmp_path):
    store = _store(tmp_path)
    service = ApiQuerySafetyService(store, FakeCCRuntime("not json"))

    with pytest.raises(ApiQuerySafetyUnavailable):
        await service.classify("测试问题", user_id="user-1", conversation_id="conv-1")

    with store.connect() as conn:
        row = conn.execute(
            "SELECT status, detail_json FROM operation_logs WHERE operation = 'api_query_safety'"
        ).fetchone()
    assert row["status"] == "unavailable"
    assert "ValueError" in row["detail_json"]


@pytest.mark.asyncio
async def test_disabled_api_query_safety_is_explicit_pass(tmp_path):
    service = ApiQuerySafetyService(_store(tmp_path), None, enabled=False)
    result = await service.classify("测试问题", user_id="user-1", conversation_id="conv-1")
    assert result.decision == "pass"
    assert result.reason_code == "safety_disabled"
