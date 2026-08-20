from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlparse
from uuid import uuid4

from claude_code_backend import LocalAgentService
from claude_code_backend.models import ChatRequest as LocalAgentChatRequest

from personal_news_agent.core.models import ChatResponse, FocusObject, SearchResult, TimeRange
from personal_news_agent.skills.base import SkillContext
from personal_news_agent.services.chat_understanding import (
    categories_for_message,
    extract_ordinal,
    is_contextual_followup,
    query_from_message,
    time_range_from_message,
)
from personal_news_agent.services.llm import LLMClient
from personal_news_agent.services.api_query_safety import (
    ApiQuerySafetyUnavailable,
    TRUSTED_SENSITIVE_NEWS_DOMAINS,
)
from personal_news_agent.services.article_fetch import canonicalize_url
from personal_news_agent.services.cc_runtime import (
    NEWS_CONVERSATION_RESEARCH_SKILL_NAME,
    NEWS_RELATED_EXPLORATION_SKILL_NAME,
)
from personal_news_agent.services.model_config import DEFAULT_LOGICAL_MODEL, DEFAULT_RUNTIME_MODEL, get_model_option
from personal_news_agent.services.search import (
    UnifiedSearchService,
    search_result_matches_subject,
    search_result_matches_terms,
)
from personal_news_agent.services.store import NewsStore


RELATED_QUERY_MIN = 3
RELATED_QUERY_MAX = 5
TOPIC_DRIFT_NOTICE = "提示：这条追问和当前关注主题关联较弱，我会照常回答，但不会因此更改当前主题或新增关注卡片。"
MULTI_FOCUS_DRIFT_NOTICE = "提示：这条消息里包含多个彼此关联较弱的热点，我会照常分别回答，但不会把它们合并成同一个主题或新增关注卡片。"
TOPIC_TEMPLATE_PHRASES = (
    "围绕",
    "围绕热点事件",
    "热点资讯",
    "热点事件",
    "中新网相关热点",
    "相关热点",
    "基于资讯",
    "展开",
    "继续深挖",
    "深度挖掘",
    "深挖",
    "做一次",
    "按",
    "告诉我发生了什么",
    "告诉我后续应该重点盯哪些变化",
    "告诉我",
    "发生了什么",
    "为什么重要",
    "后续看什么",
    "和后续观察点",
    "后续观察点",
    "后续观察",
    "后续应该重点盯哪些变化",
    "后续回应或新进展",
    "是否出现后续回应或新进展",
    "是否出现新的权威来源",
    "新的权威来源",
    "权威来源",
    "是否出现",
    "最近有什么值得关注的变化",
    "今天有哪些值得关注的新变化",
    "值得关注的新变化",
    "最新新闻",
    "按来源搜索",
    "正文抓取",
    "证据合并",
    "和事件线处理",
    "事件线处理",
    "最新进展",
    "关键主体",
    "不确定性总结",
    "整理成专题",
    "和关系网观察重点",
    "事件线",
    "关系网观察重点",
    "观察重点",
    "设为跟踪主题",
    "保存跟踪",
    "跟踪主题",
    "持续跟踪",
    "跟踪",
    "追踪",
    "继续观察",
    "给我结论",
    "给我",
    "结论",
    "证据",
    "观察后续是否影响市场行业公众服务或地方执行",
    "观察后续是否影响市场、行业、公众服务或地方执行",
    "基于我的兴趣和当前推荐",
    "今日简报",
    "一版今日简报",
    "已把",
    "把",
)


@dataclass(frozen=True)
class SearchQueryPlan:
    query: str
    primary_subject: str
    required_terms: list[str]
    keywords: list[str]
    source: str = "fallback"

class NewsChatService:
    def __init__(
        self,
        store: NewsStore,
        search_service: UnifiedSearchService,
        llm_client: LLMClient | None = None,
        native_ingestion: Any | None = None,
        deep_dive: Any | None = None,
        topic_views: Any | None = None,
        topic_agent: Any | None = None,
        scheduled_tasks: Any | None = None,
        content_moderation: Any | None = None,
        local_agent: LocalAgentService | None = None,
        cc_runtime: Any | None = None,
        api_query_safety: Any | None = None,
        skill_registry: Any | None = None,
        services: dict[str, Any] | None = None,
    ):
        self.store = store
        self.search_service = search_service
        self.llm_client = llm_client or LLMClient()
        self.native_ingestion = native_ingestion
        self.deep_dive = deep_dive
        self.topic_views = topic_views
        self.topic_agent = topic_agent
        self.scheduled_tasks = scheduled_tasks
        self.content_moderation = content_moderation
        self.local_agent = local_agent or LocalAgentService()
        self.cc_runtime = cc_runtime
        self.api_query_safety = api_query_safety
        self.skill_registry = skill_registry
        self.services = services or {}
        self.topic_drift_notice = TOPIC_DRIFT_NOTICE

    async def chat(
        self,
        conversation_id: str | None,
        message: str,
        topic: str | None = None,
        category_scope: list[str] | None = None,
        use_llm: bool = False,
        user_id: str = "default",
        allow_web_search: bool = False,
        model_key: str = DEFAULT_LOGICAL_MODEL,
        conversation_mode: str = "auto",
        enforce_api_safety: bool = False,
    ) -> ChatResponse:
        conv_id = conversation_id or f"conv_{uuid4().hex[:12]}"
        topic, category_scope = self._resolve_conversation_context(conv_id, message, topic, category_scope, user_id)
        save_topic = self._request_topic_for_save(message, topic)
        if enforce_api_safety and not self.api_query_safety:
            raise ApiQuerySafetyUnavailable("API query safety service is unavailable")
        if enforce_api_safety:
            history = _conversation_history_text(
                self._conversation_memory(conv_id, user_id, current_limit=4, recent_limit=0),
                turn_limit=4,
                question_limit=260,
                answer_limit=800,
            )
            safety = await self.api_query_safety.classify(
                message,
                user_id=user_id,
                conversation_id=conv_id,
                history=history,
            )
            if safety.decision == "refuse":
                response = ChatResponse(
                    conversation_id=conv_id,
                    answer=safety.response,
                    markdown=safety.response,
                    context_relation=f"api_query_safety_{safety.decision}",
                    topic=topic,
                    category_scope=category_scope or [],
                    focus_object=FocusObject(type="api_query_safety", text=safety.decision),
                    required_context_items=list(safety.categories),
                    research_trace=[
                        {
                            "stage": "API 请求安全判断",
                            "status": "completed",
                            "message": "已按 API 安全策略生成受约束回答。",
                        }
                    ],
                    skill_result={
                        "type": "api_query_safety",
                        "decision": safety.decision,
                        "categories": list(safety.categories),
                        "risk_level": safety.risk_level,
                        "reason_code": safety.reason_code,
                    },
                )
                self._save_response_turn(response, message, user_id, save_topic, category_scope)
                return response
            if safety.decision == "safe_answer":
                answer = safety.response
                trace = [
                    {
                        "stage": "API 请求安全判断",
                        "status": "completed",
                        "message": "已进入可信来源受约束回答链路。",
                    }
                ]
                if self.cc_runtime and getattr(self.cc_runtime, "configured", False):
                    try:
                        research = await self.cc_runtime.run(
                            message=(
                                "下列前置口径由系统在最终输出时统一添加，你不要重复它，只回答用户的具体问题。"
                                "仅依据本轮可信来源检索结果总结，"
                                "保持中国国家立场和克制、准确的措辞；证据不足时明确说明，不得编造。\n"
                                f"前置口径：{safety.response}\n用户原问题：{message}"
                            ),
                            query=message[:500],
                            topic=topic,
                            category_scope=category_scope or [],
                            time_range=None,
                            history=history,
                            allow_web_search=True,
                            allow_local_search=False,
                            logical_model_key=model_key,
                            logical_model_name=get_model_option(model_key).name,
                            skill_names=None,
                            strict_json_output=False,
                            max_turns=5,
                            effort="low",
                            builtin_web_search_limit=2,
                            allow_everyday_tools=True,
                            require_builtin_web_search=True,
                            apply_sensitive_fact_guard=True,
                            trusted_web_domains=list(TRUSTED_SENSITIVE_NEWS_DOMAINS),
                        )
                        answer = f"{safety.response}\n\n{research.answer.strip()}".strip()
                        trace.extend(research.trace)
                    except Exception as exc:
                        self.store.log(
                            "api_query_safety_research",
                            "error",
                            target=conv_id,
                            detail={
                                "error_type": type(exc).__name__,
                                "error": str(exc)[:500],
                                "categories": list(safety.categories),
                            },
                        )
                        trace.append(
                            {
                                "stage": "可信来源检索",
                                "status": "warning",
                                "message": "暂未取得可核实的权威资料，已保留必要口径。",
                                "error_type": type(exc).__name__,
                            }
                        )
                response = ChatResponse(
                    conversation_id=conv_id,
                    answer=answer,
                    markdown=answer,
                    context_relation="api_query_safety_safe_answer",
                    topic=topic,
                    category_scope=category_scope or [],
                    focus_object=FocusObject(type="api_query_safety", text=safety.decision),
                    required_context_items=list(safety.categories),
                    research_trace=trace,
                    skill_result={
                        "type": "api_query_safety",
                        "decision": safety.decision,
                        "categories": list(safety.categories),
                        "risk_level": safety.risk_level,
                        "reason_code": safety.reason_code,
                        "trusted_domains": list(TRUSTED_SENSITIVE_NEWS_DOMAINS),
                    },
                )
                self._save_response_turn(response, message, user_id, save_topic, category_scope)
                return response
        moderation_response = await self._moderate_query(conv_id, message, user_id)
        if moderation_response:
            self._save_response_turn(moderation_response, message, user_id, save_topic, category_scope)
            return moderation_response
        skill_response = await self._skill_response(conv_id, message, user_id, topic, category_scope, allow_web_search)
        if skill_response:
            self._save_response_turn(skill_response, message, user_id, save_topic, category_scope)
            return skill_response
        schedule_response = await self._schedule_command_response(conv_id, user_id, message)
        if schedule_response:
            self._save_response_turn(schedule_response, message, user_id, save_topic, category_scope)
            return schedule_response
        topic_response = await self._topic_agent_response(conv_id, user_id, message)
        ordinal = extract_ordinal(message) if not topic_response else None
        if topic_response:
            response = topic_response
        elif ordinal:
            response = await self._article_followup(conv_id, message, ordinal)
        elif use_llm:
            use_general_chat = conversation_mode == "general" or (
                conversation_mode == "auto" and _is_general_conversation(message, topic)
            )
            if use_general_chat:
                response = await self._general_chat(
                    conv_id,
                    message,
                    user_id=user_id,
                    allow_web_search=allow_web_search,
                    model_key=model_key,
                )
                save_topic = None
            else:
                response = await self._research_chat(
                    conv_id,
                    message,
                    topic,
                    category_scope,
                    user_id=user_id,
                    allow_web_search=allow_web_search,
                    model_key=model_key,
                )
        else:
            response = await self._news_search(
                conv_id,
                message,
                topic,
                category_scope,
                use_llm,
                user_id,
                allow_web_search,
                model_key,
            )
        self._save_response_turn(response, message, user_id, save_topic, category_scope)
        return response

    async def related_search(
        self,
        conversation_id: str | None,
        query: str,
        topic: str | None = None,
        category_scope: list[str] | None = None,
        user_id: str = "default",
        max_queries: int = RELATED_QUERY_MAX,
        allow_web_search: bool = False,
        on_trace: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        save_turn: bool = True,
    ) -> ChatResponse:
        conv_id = conversation_id or f"conv_{uuid4().hex[:12]}"
        message = f"/related {query}".strip()
        topic, category_scope = self._resolve_conversation_context(conv_id, query, topic, category_scope, user_id)
        requested_focus = _related_base_query(query_from_message(query, topic), query)
        base_query = _related_context_query(topic, requested_focus)
        categories = categories_for_message(base_query, topic, category_scope)
        query_limit = _related_query_limit(max_queries)
        trace: list[dict[str, Any]] = []
        await _add_trace(
            trace,
            {
                "stage": "关联消歧",
                "status": "running",
                "message": f"正在结合当前主题【{topic or '未设定'}】理解【{requested_focus}】。",
            },
            on_trace,
        )
        memory = [
            turn
            for turn in self._conversation_memory(conv_id, user_id, current_limit=6, recent_limit=0)
            if not str(turn.get("user_message") or "").strip().lower().startswith("/related")
        ]
        history = _conversation_history_text(memory, turn_limit=4, question_limit=260, answer_limit=1_400)

        if self.cc_runtime and getattr(self.cc_runtime, "configured", False):
            try:
                runtime_settings = getattr(self.cc_runtime, "settings", None)
                await _add_trace(
                    trace,
                    {
                        "stage": "关联消歧",
                        "status": "completed",
                        "message": f"已将检索焦点解析为【{base_query}】，保留当前主题不变。",
                    },
                    on_trace,
                )
                runtime_result = await self.cc_runtime.run(
                    message=(
                        "用户正在执行相关新闻延展。"
                        f"当前对话主题是【{topic or base_query}】，用户指定焦点是【{requested_focus}】。"
                        "请先结合近期对话消歧这个焦点，再说明它与当前主题的具体关系、近况和后续观察点。"
                    ),
                    query=base_query,
                    topic=topic or base_query,
                    category_scope=categories,
                    time_range=None,
                    history=history,
                    allow_web_search=allow_web_search,
                    logical_model_key=DEFAULT_LOGICAL_MODEL,
                    skill_names=[NEWS_RELATED_EXPLORATION_SKILL_NAME],
                    timeout_seconds=max(
                        180.0,
                        float(getattr(runtime_settings, "cc_runtime_timeout_seconds", 150.0)),
                    ),
                    max_turns=max(10, int(getattr(runtime_settings, "cc_runtime_max_turns", 6))),
                    builtin_web_search_limit=3,
                    on_trace=on_trace,
                )
                runtime_candidates = _enrich_from_store(
                        self.store,
                        [
                            *runtime_result.results,
                            *_runtime_declared_link_results(
                                runtime_result.answer,
                                categories[0] if categories else "all",
                            ),
                        ],
                    )
                runtime_results = _rank_for_chat(
                    _filter_related_runtime_results(runtime_candidates, topic, requested_focus),
                    base_query,
                )[:12]
                evidence = _evidence_payload(self.store, runtime_results)
                related_queries = _runtime_related_queries(base_query, runtime_result.queries, query_limit)
                grouped = _related_groups_from_runtime(related_queries, runtime_results)
                runtime_trace = [*trace, *runtime_result.trace]
                builtin_web_calls = int(runtime_result.provider_metadata.get("builtin_web_calls") or 0)
                total_tool_calls = len(runtime_result.queries) + builtin_web_calls
                runtime_trace.append(
                    {
                        "stage": "Agent 主控",
                        "status": "completed",
                        "message": f"完成 {total_tool_calls} 次只读检索，确认焦点与当前主题的关系。",
                        "count": total_tool_calls,
                    }
                )
                response = ChatResponse(
                    conversation_id=conv_id,
                    answer=runtime_result.answer,
                    markdown=runtime_result.answer,
                    context_relation="related_search_cc_runtime",
                    topic=topic or base_query,
                    category_scope=categories or [],
                    focus_object=FocusObject(type="topic", text=base_query),
                    required_context_items=[
                        "current_topic",
                        "requested_focus",
                        "conversation_history",
                        "local_news_search",
                        "web_search",
                        "retrieved_evidence",
                    ],
                    recommendations=runtime_results[:8],
                    research_trace=runtime_trace,
                    evidence=evidence,
                    expanded_queries=related_queries,
                    mind_map=_related_mind_map_payload(
                        base_query,
                        grouped,
                        evidence,
                        "cc_runtime",
                        active_topic=topic or base_query,
                        requested_focus=requested_focus,
                        conclusion=_related_conclusion(runtime_result.answer),
                        runtime_queries=runtime_result.queries,
                        builtin_web_calls=builtin_web_calls,
                    ),
                )
                if save_turn:
                    self._save_response_turn(response, message, user_id, topic or base_query, category_scope)
                return response
            except Exception as exc:
                failure_message = _related_runtime_failure_message(exc)
                self.store.log(
                    "cc_runtime_related",
                    "error",
                    base_query,
                    {"error_type": type(exc).__name__, "error": str(exc)[:500]},
                )
                await _add_trace(
                    trace,
                    {
                        "stage": "Agent 主控",
                        "status": "fallback",
                        "message": failure_message,
                    },
                    on_trace,
                )

        await _add_trace(
            trace,
            {
                "stage": "相关规划",
                "status": "running",
                "message": "正在生成带当前主题约束的相关检索词。",
            },
            on_trace,
        )
        related_queries, planner_source = await self._plan_related_queries(
            base_query,
            categories,
            user_id=user_id,
            max_queries=query_limit,
        )
        await _add_trace(
            trace,
            {
                "stage": "相关规划",
                "status": "completed" if planner_source == "local_agent" else "fallback",
                "message": f"生成 {len(related_queries)} 个相关检索词。",
                "count": len(related_queries),
            },
            on_trace,
        )

        grouped: list[dict[str, Any]] = []
        merged_results: list[SearchResult] = []
        for item in related_queries:
            related_query = item.get("query") or ""
            if not related_query:
                continue
            results = await self.search_service.search(
                query=related_query,
                category_scope=categories,
                source_scope=None,
                time_range=None,
                max_results=6,
                include_remote=allow_web_search,
            )
            ranked = _rank_for_chat(
                _filter_related_runtime_results(
                    _enrich_from_store(self.store, results),
                    topic,
                    requested_focus,
                ),
                related_query,
            )[:5]
            grouped.append(
                {
                    "query": related_query,
                    "reason": item.get("reason") or "",
                    "relation_type": item.get("relation_type") or "other",
                    "relation_label": item.get("relation_label") or _related_relation_label(item.get("relation_type") or "other"),
                    "count": len(ranked),
                    "items": ranked,
                }
            )
            merged_results.extend(ranked)
        merged_results = _merge_results(merged_results)[:12]
        evidence = _evidence_payload(self.store, merged_results)
        await _add_trace(
            trace,
            {
                "stage": "自动搜索",
                "status": "completed",
                "message": f"已完成 {len(grouped)} 组相关搜索，合并 {len(evidence)} 条证据。",
                "count": len(evidence),
            },
            on_trace,
        )

        expanded_queries = [
            {
                "query": item.get("query") or "",
                "rationale": item.get("reason") or "",
                "relation_type": item.get("relation_type") or "other",
                "relation_label": item.get("relation_label") or _related_relation_label(item.get("relation_type") or "other"),
            }
            for item in related_queries
            if item.get("query")
        ]
        answer = _related_search_answer(base_query, grouped, evidence, planner_source)
        mind_map = _related_mind_map_payload(
            base_query,
            grouped,
            evidence,
            planner_source,
            active_topic=topic or base_query,
            requested_focus=requested_focus,
            conclusion=_related_conclusion(answer),
        )
        response = ChatResponse(
            conversation_id=conv_id,
            answer=answer,
            markdown=answer,
            context_relation="related_search",
            topic=topic or base_query,
            category_scope=categories or [],
            focus_object=FocusObject(type="topic", text=base_query),
            required_context_items=["local_agent_related_queries", "retrieved_evidence"],
            recommendations=merged_results[:8],
            research_trace=trace,
            evidence=evidence,
            expanded_queries=expanded_queries,
            mind_map=mind_map,
        )
        if save_turn:
            self._save_response_turn(response, message, user_id, topic or base_query, category_scope)
        return response

    async def chat_events(
        self,
        conversation_id: str | None,
        message: str,
        topic: str | None = None,
        category_scope: list[str] | None = None,
        use_llm: bool = False,
        user_id: str = "default",
        allow_web_search: bool = False,
        model_key: str = DEFAULT_LOGICAL_MODEL,
    ) -> AsyncIterator[dict[str, Any]]:
        conv_id = conversation_id or f"conv_{uuid4().hex[:12]}"
        topic, category_scope = self._resolve_conversation_context(conv_id, message, topic, category_scope, user_id)
        save_topic = self._request_topic_for_save(message, topic)
        yield {"type": "start", "conversation_id": conv_id, "message": "开始处理问题。"}
        moderation_response = await self._moderate_query(conv_id, message, user_id)
        if moderation_response:
            self._save_response_turn(moderation_response, message, user_id, save_topic, category_scope)
            yield {"type": "final", "response": moderation_response.model_dump(mode="json")}
            return
        if message.strip().startswith("/") and self.skill_registry:
            skill_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

            async def emit_skill_trace(item: dict[str, Any]) -> None:
                await skill_queue.put({"type": "trace", "item": item})

            async def run_skill() -> None:
                try:
                    response = await self._skill_response(
                        conv_id,
                        message,
                        user_id,
                        topic,
                        category_scope,
                        allow_web_search,
                        emit_skill_trace,
                    )
                    if response:
                        self._save_response_turn(response, message, user_id, save_topic, category_scope)
                        await skill_queue.put({"type": "final", "response": response.model_dump(mode="json")})
                    else:
                        await skill_queue.put({"type": "error", "message": "Skill 未返回结果。"})
                except Exception as exc:
                    await skill_queue.put({"type": "error", "message": str(exc)})

            skill_task = asyncio.create_task(run_skill())
            try:
                while True:
                    event = await skill_queue.get()
                    yield event
                    if event["type"] in {"final", "error"}:
                        break
            finally:
                if not skill_task.done():
                    skill_task.cancel()
            return
        schedule_response = await self._schedule_command_response(conv_id, user_id, message)
        if schedule_response:
            self._save_response_turn(schedule_response, message, user_id, save_topic, category_scope)
            yield {"type": "trace", "item": {"stage": "定时任务", "status": "completed", "message": schedule_response.context_relation}}
            yield {"type": "final", "response": schedule_response.model_dump(mode="json")}
            return
        topic_response = await self._topic_agent_response(conv_id, user_id, message)
        if topic_response:
            for item in topic_response.research_trace:
                yield {"type": "trace", "item": item}
            self._save_response_turn(topic_response, message, user_id, save_topic, category_scope)
            yield {"type": "final", "response": topic_response.model_dump(mode="json")}
            return
        ordinal = extract_ordinal(message)
        if ordinal or not use_llm:
            response = await (
                self._article_followup(conv_id, message, ordinal)
                if ordinal
                else self._news_search(
                    conv_id,
                    message,
                    topic,
                    category_scope,
                    use_llm,
                    user_id,
                    allow_web_search,
                    model_key,
                )
            )
            self._save_response_turn(response, message, user_id, save_topic, category_scope)
            yield {"type": "final", "response": response.model_dump(mode="json")}
            return

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def emit_trace(item: dict[str, Any]) -> None:
            await queue.put({"type": "trace", "item": item})

        async def run_pipeline() -> None:
            try:
                general_conversation = _is_general_conversation(message, topic)
                if general_conversation:
                    response = await self._general_chat(
                        conv_id,
                        message,
                        emit_trace,
                        user_id,
                        allow_web_search,
                        model_key,
                    )
                else:
                    response = await self._research_chat(
                        conv_id,
                        message,
                        topic,
                        category_scope,
                        emit_trace,
                        user_id,
                        allow_web_search,
                        model_key,
                    )
                self._save_response_turn(
                    response,
                    message,
                    user_id,
                    None if general_conversation else save_topic,
                    category_scope,
                )
                await queue.put({"type": "final", "response": response.model_dump(mode="json")})
            except Exception as exc:
                await queue.put({"type": "error", "message": str(exc)})

        task = asyncio.create_task(run_pipeline())
        try:
            while True:
                event = await queue.get()
                yield event
                if event["type"] in {"final", "error"}:
                    break
        finally:
            if not task.done():
                task.cancel()

    async def _skill_response(
        self,
        conversation_id: str,
        message: str,
        user_id: str,
        topic: str | None = None,
        category_scope: list[str] | None = None,
        allow_web_search: bool = False,
        on_trace: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ChatResponse | None:
        text = message.strip()
        if not text.startswith("/") or not self.skill_registry:
            return None
        topic, category_scope = self._skill_context_from_turns(conversation_id, text, user_id, topic, category_scope)
        command = text.split(maxsplit=1)[0].lower()
        if on_trace:
            await on_trace(
                {
                    "stage": "场景 Skill",
                    "status": "running",
                    "message": f"正在执行 {command} 场景工作流。",
                }
            )
        try:
            result = await self.skill_registry.execute(
                text,
                SkillContext(
                    services=self.services,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    topic=topic,
                    category_scope=category_scope,
                    allow_web_search=allow_web_search,
                    on_trace=on_trace,
                ),
            )
        except ValueError as exc:
            answer = str(exc)
            return ChatResponse(
                conversation_id=conversation_id,
                answer=answer,
                markdown=answer,
                context_relation="skill_error",
                focus_object=FocusObject(type="skill", text=text.split()[0] if text.split() else text),
                required_context_items=["skill_registry"],
            )
        payload = result.data or {}
        skill_trace = {
            "stage": "场景 Skill",
            "status": "completed",
            "message": f"{result.title}工作流已完成。",
        }
        if on_trace:
            await on_trace(skill_trace)
        payload["research_trace"] = [skill_trace, *(payload.get("research_trace") or [])]
        answer = _skill_answer(result.title, result.message, payload)
        response_topic = _skill_context_topic(result.command, payload)
        focus_payload = payload.get("focus_object")
        if isinstance(focus_payload, dict):
            focus_object = FocusObject.model_validate(focus_payload)
        else:
            focus_type = "topic" if response_topic else "skill"
            focus_object = FocusObject(type=focus_type, text=response_topic or result.title)
        recommendations = [
            SearchResult.model_validate(item)
            for item in (payload.get("recommendations") or [])
            if isinstance(item, dict)
        ]
        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            markdown=answer,
            context_relation=payload.get("context_relation") or f"skill:{result.command}",
            topic=response_topic,
            category_scope=payload.get("category_scope") or [],
            focus_object=focus_object,
            required_context_items=payload.get("required_context_items") or ["skill_registry"],
            recommendations=recommendations,
            research_trace=payload.get("research_trace") or [],
            evidence=payload.get("evidence") or [],
            expanded_queries=payload.get("expanded_queries") or [],
            event_line=payload.get("event_line"),
            mind_map=payload.get("mind_map"),
            skill_result={
                "command": result.command,
                "title": result.title,
                "message": result.message,
                "data": payload,
            },
        )

    async def _general_chat(
        self,
        conversation_id: str,
        message: str,
        on_trace: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        user_id: str = "default",
        allow_web_search: bool = False,
        model_key: str = DEFAULT_LOGICAL_MODEL,
    ) -> ChatResponse:
        trace: list[dict[str, Any]] = []
        selected_model = get_model_option(model_key, getattr(self.llm_client, "settings", None))
        await _add_trace(
            trace,
            {
                "stage": "理解问题",
                "status": "completed",
                "message": "识别为通用对话，不加载业务 Skill，由 Agent 自主选择生活服务或外部搜索工具。",
            },
            on_trace,
        )
        if self.cc_runtime and getattr(self.cc_runtime, "configured", False):
            await _add_trace(
                trace,
                {"stage": "Agent 主控", "status": "running", "message": "正在结合对话上下文组织回答。"},
                on_trace,
            )
            history = ""
            if not _is_social_smalltalk(message):
                history = _conversation_history_text(
                    self._conversation_memory(conversation_id, user_id, current_limit=4, recent_limit=0),
                    turn_limit=4,
                    question_limit=400,
                    answer_limit=1_000,
                )
            try:
                result = await self.cc_runtime.run(
                    message=message,
                    query=message,
                    topic=None,
                    category_scope=[],
                    time_range=None,
                    history=history,
                    allow_web_search=allow_web_search,
                    logical_model_key=selected_model.key,
                    logical_model_name=selected_model.name,
                    skill_names=[],
                    allow_local_search=False,
                    max_turns=6,
                    builtin_web_search_limit=2,
                    allow_everyday_tools=allow_web_search,
                    require_builtin_web_search=False,
                    on_trace=on_trace,
                )
                declared = _runtime_declared_link_results(result.answer, "all")
                answer = result.answer
                trace.extend(result.trace)
                final_trace = {
                    "stage": "生成回答",
                    "status": "completed",
                    "message": "Agent 已完成通用对话回答。",
                }
                await _add_trace(trace, final_trace, on_trace)
                return ChatResponse(
                    conversation_id=conversation_id,
                    answer=answer,
                    markdown=answer,
                    context_relation="general_conversation_cc_runtime",
                    focus_object=FocusObject(type="conversation", text="通用对话"),
                    required_context_items=[
                        "cc_runtime",
                        "conversation_history",
                        "everyday_capabilities",
                        "web_search",
                    ],
                    recommendations=declared[:8],
                    research_trace=[trace[0], *result.trace, final_trace],
                    evidence=_evidence_payload(self.store, declared),
                    expanded_queries=result.queries[:8],
                )
            except Exception as exc:
                self.store.log("cc_runtime_general", "error", message[:120], {"error_type": type(exc).__name__})
        answer = "你好，我是你的个人资讯 Agent。你可以直接聊天，也可以让我检索新闻、核查事实、生成事件图谱或定时报告。"
        await _add_trace(
            trace,
            {"stage": "Agent 主控", "status": "fallback", "message": "Agent 主控暂不可用，返回基础说明。"},
            on_trace,
        )
        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            markdown=answer,
            context_relation="general_conversation_fallback",
            focus_object=FocusObject(type="conversation", text="通用对话"),
            required_context_items=["cc_runtime"],
            research_trace=trace,
        )

    def _skill_context_from_turns(
        self,
        conversation_id: str,
        text: str,
        user_id: str,
        topic: str | None,
        category_scope: list[str] | None,
    ) -> tuple[str | None, list[str] | None]:
        command = text.split(maxsplit=1)[0].lower() if text.split() else ""
        if command not in {"/report", "/brief"}:
            return topic, category_scope
        if _skill_command_has_topic_arg(text):
            return topic, category_scope
        if command == "/report":
            last = self.store.last_turn(conversation_id, user_id=user_id)
            if not last:
                return topic, category_scope
            return None, last.get("category_scope") or category_scope
        last = self.store.last_turn(conversation_id, user_id=user_id)
        if not last:
            return topic, category_scope
        if topic and not _is_default_brief_topic(topic):
            return topic, category_scope
        return last.get("topic") or topic, last.get("category_scope") or category_scope

    async def _topic_agent_response(self, conversation_id: str, user_id: str, message: str) -> ChatResponse | None:
        if not self.topic_agent:
            return None
        is_create_command, inline_topic = _topic_create_request(message)
        if is_create_command and not inline_topic:
            answer = "好的，请发送要长期关注的主题。下一条消息会保存为新关注。"
            return ChatResponse(
                conversation_id=conversation_id,
                answer=answer,
                markdown=answer,
                context_relation="topic_create_pending",
                focus_object=FocusObject(type="topic_create_pending", text="pending"),
                required_context_items=["next_message_as_topic"],
                research_trace=[
                    {"stage": "关注创建", "status": "waiting", "message": "等待下一条消息作为长期关注主题。"}
                ],
            )
        if inline_topic:
            topic_text = inline_topic
        elif self._topic_create_is_pending(conversation_id, user_id):
            topic_text = message
        else:
            return None
        try:
            result = await self.topic_agent.create_topic_from_text(
                user_id=user_id,
                text=topic_text,
                refresh_now=True,
                conversation_id=conversation_id,
            )
        except ValueError:
            return None
        if not result:
            return None
        topic = result["topic"]
        task = result.get("task")
        refresh = result.get("refresh") or {}
        ingest = refresh.get("ingest") or {}
        view = refresh.get("topic_view") or {}
        article_count = (view.get("build") or {}).get("article_count") or len(view.get("articles") or [])
        event_count = len(((view.get("event_line") or {}).get("items")) or [])
        answer = (
            f"已创建主题「{topic['title']}」，并保存为持续跟踪。\n\n"
            f"- 更新节奏：{topic.get('refresh_schedule') or '*/20 * * * *'}\n"
            f"- 抓取入库：发现 {ingest.get('discovered_count', 0)} 条，正文 {ingest.get('fetched_count', 0)} 条\n"
            f"- 专题视图：{article_count} 条证据，{event_count} 个事件节点\n\n"
            "后续可以直接问这个主题的最新变化、关键人物/球队/公司、影响链或让我生成报告。"
        )
        trace = [
            {"stage": "主题识别", "status": "completed", "message": f"识别为长期主题：{topic['title']}"},
            {"stage": "任务沉淀", "status": "completed", "message": f"已保存持续跟踪任务：{(task or {}).get('id') or '已存在'}"},
            {
                "stage": "抓取与视图",
                "status": "completed" if refresh.get("refreshed") else "skipped",
                "message": f"发现 {ingest.get('discovered_count', 0)} 条，专题证据 {article_count} 条。",
            },
        ]
        if refresh.get("errors"):
            trace.append({"stage": "刷新提示", "status": "warning", "message": "；".join(refresh["errors"][:2])})
        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            markdown=answer,
            context_relation="topic_agent_created",
            focus_object=FocusObject(type="topic", target_id=topic["id"], text=topic["title"]),
            required_context_items=["topic_definition", "scheduled_task", "topic_refresh"],
            research_trace=trace,
            event_line=view.get("event_line"),
        )

    async def _schedule_command_response(self, conversation_id: str, user_id: str, message: str) -> ChatResponse | None:
        if not self.scheduled_tasks or not str(message or "").strip().startswith("/schedule"):
            return None
        try:
            result = await self.scheduled_tasks.create_from_schedule_message(user_id=user_id, message=message)
        except ValueError as exc:
            answer = f"定时任务没有创建成功：{exc}"
            return ChatResponse(
                conversation_id=conversation_id,
                answer=answer,
                markdown=answer,
                context_relation="scheduled_push_invalid",
                focus_object=FocusObject(type="scheduled_task", text="/schedule"),
                required_context_items=["schedule_command"],
                research_trace=[{"stage": "定时任务解析", "status": "error", "message": str(exc)}],
            )
        task = result["task"]
        scheduled_conversation = result["conversation"]
        answer = result["answer"]
        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            markdown=answer,
            context_relation="scheduled_push_created",
            focus_object=FocusObject(type="scheduled_task", target_id=task["id"], text=(task.get("topics") or [""])[0]),
            required_context_items=["scheduled_task", "scheduled_push_conversation"],
            research_trace=[
                {"stage": "定时任务解析", "status": "completed", "message": f"已解析为 cron：{task['schedule']}"},
                {"stage": "任务落库", "status": "completed", "message": f"任务 {task['id']} 已保存。"},
                {"stage": "推送对话", "status": "completed", "message": f"将推送到 {scheduled_conversation['title']}。"},
            ],
        )

    def _topic_create_is_pending(self, conversation_id: str, user_id: str) -> bool:
        last = self.store.last_turn(conversation_id, user_id=user_id)
        response = (last or {}).get("response") or {}
        return response.get("context_relation") == "topic_create_pending"

    async def _moderate_query(
        self,
        conversation_id: str,
        message: str,
        user_id: str,
    ) -> ChatResponse | None:
        if not self.content_moderation or not getattr(self.content_moderation, "configured", False):
            return None
        try:
            result = await asyncio.to_thread(
                self.content_moderation.check_query_text,
                message,
                account_id=user_id,
                data_id=conversation_id,
            )
        except Exception as exc:
            self.store.log(
                "query_content_moderation",
                "unavailable",
                conversation_id,
                {
                    "provider": "aliyun_text_moderation_plus",
                    "service": getattr(self.content_moderation, "query_service", None),
                    "error_type": type(exc).__name__,
                    "provider_code": getattr(exc, "provider_code", None),
                    "request_id": getattr(exc, "request_id", None),
                    "fail_open": bool(getattr(self.content_moderation, "fail_open", True)),
                },
            )
            if getattr(self.content_moderation, "fail_open", True):
                return None
            answer = "输入安全检测服务暂时不可用，请稍后再试。"
            return ChatResponse(
                conversation_id=conversation_id,
                answer=answer,
                markdown=answer,
                context_relation="query_moderation_unavailable",
                focus_object=FocusObject(type="moderation", text="unavailable"),
                required_context_items=["llm_query_moderation"],
                research_trace=[
                    {
                        "stage": "输入安全检测",
                        "status": "error",
                        "message": "安全检测服务暂时不可用，未进入模型处理。",
                    }
                ],
            )
        if result.allowed:
            self.store.log(
                "query_content_moderation",
                "allowed",
                conversation_id,
                {
                    "provider": "aliyun_text_moderation_plus",
                    "service": getattr(self.content_moderation, "query_service", None),
                    "risk_level": result.risk_level,
                    "label": result.label,
                    "request_id": result.request_id,
                },
            )
            return None
        self.store.log(
            "query_content_moderation",
            "blocked",
            conversation_id,
            {
                "provider": "aliyun_text_moderation_plus",
                "service": getattr(self.content_moderation, "query_service", None),
                "risk_level": result.risk_level,
                "label": result.label,
                "request_id": result.request_id,
            },
        )
        answer = "这条问题没有通过内容安全检测，请换一种问法后再试。"
        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            markdown=answer,
            context_relation="query_moderation_blocked",
            focus_object=FocusObject(type="moderation", text=result.label or result.risk_level or "blocked"),
            required_context_items=["llm_query_moderation"],
            research_trace=[
                {
                    "stage": "输入安全检测",
                    "status": "blocked",
                    "message": result.description or result.message or "用户输入未通过内容安全检测。",
                    "label": result.label,
                    "risk_level": result.risk_level,
                    "request_id": result.request_id,
                }
            ],
        )



    def _resolve_conversation_context(
        self,
        conversation_id: str,
        message: str,
        topic: str | None,
        category_scope: list[str] | None,
        user_id: str,
    ) -> tuple[str | None, list[str] | None]:
        topic = None if _is_topic_placeholder(topic) else topic
        # A slash skill with an explicit topic argument owns its topic. Do not
        # let the conversation's previous turn replace it before the skill
        # context resolver gets a chance to consume the argument.
        if str(message or "").strip().lower().startswith(("/report", "/brief")) and (
            topic or _skill_command_has_topic_arg(message)
        ):
            return topic, category_scope
        explicit_focus = _explicit_focus_from_message(message)
        if explicit_focus:
            if topic and _compact_topic_text(topic) != _compact_topic_text(explicit_focus):
                return topic, None
            turns = self.store.list_turns(conversation_id, user_id=user_id, limit=3)
            last = _last_turn_with_topic(turns)
            previous_topic = (last or {}).get("topic") or _focus_topic_text(last or {})
            return previous_topic or topic, None
        if not is_contextual_followup(message):
            # A conversation has one stable working topic. A plain follow-up
            # question may use different wording, but should not silently
            # replace that topic; explicit /topic or API context can still do
            # so before this resolver is called.
            turns = self.store.list_turns(conversation_id, user_id=user_id, limit=12)
            last = _last_turn_with_topic(turns)
            if last:
                previous_topic = last.get("topic") or _focus_topic_text(last)
                previous_categories = last.get("category_scope") or category_scope
                return previous_topic or topic, previous_categories
            return topic, category_scope
        turns = self.store.list_turns(conversation_id, user_id=user_id, limit=12)
        if not turns:
            turns = self.store.list_recent_turns(user_id=user_id, limit=6, exclude_conversation_id=conversation_id)
        if not turns:
            return topic, category_scope
        last = _last_turn_with_topic(turns)
        if not last:
            return topic, category_scope
        previous_topic = last.get("topic") or _focus_topic_text(last)
        previous_categories = last.get("category_scope") or category_scope
        return previous_topic or topic, previous_categories

    def _request_topic_for_save(self, message: str, topic: str | None) -> str | None:
        return None if _is_topic_placeholder(topic) else topic

    def _conversation_memory(
        self,
        conversation_id: str,
        user_id: str,
        current_limit: int = 6,
        recent_limit: int = 4,
    ) -> list[dict[str, Any]]:
        current_turns = self.store.list_turns(conversation_id, user_id=user_id, limit=current_limit)
        recent_turns = self.store.list_recent_turns(
            user_id=user_id,
            limit=recent_limit,
            exclude_conversation_id=conversation_id,
        )
        return _dedupe_turns([*recent_turns, *current_turns])[-(current_limit + recent_limit):]

    async def _resolve_contextual_search_message(
        self,
        conversation_id: str,
        user_id: str,
        message: str,
        topic: str | None,
    ) -> str:
        if not is_contextual_followup(message):
            return message
        if not getattr(getattr(self.local_agent, "config", None), "enabled", True):
            return message
        history = self._conversation_memory(conversation_id, user_id, current_limit=4, recent_limit=0)
        if not history:
            return message
        recent_memory = _conversation_history_text(history, turn_limit=3, question_limit=260, answer_limit=1600)
        if recent_memory == "无":
            return message
        prompt = (
            "请把用户的上下文追问改写成一个可以独立搜索的完整中文问题。"
            "你只做指代消解和问题改写，不要回答问题。"
            "如果用户说“上面那些、这些、刚才提到的、上述”等指代词，"
            "请从 recent_memory 中找出对应的公司、品牌、人物、机构、事件或产品名称，并写入改写后的问题。"
            "不要添加 recent_memory 中没有出现的具体对象。"
            "如果无法确定指代对象，就原样返回用户问题。"
            "只返回改写后的问题本身，最多 220 个中文字符。"
        )
        request = LocalAgentChatRequest(
            session_id=_local_agent_context_session_id(conversation_id),
            user_id=user_id,
            message=prompt,
            project_context={
                "purpose": "contextual_query_rewrite",
                "current_topic": topic or "",
                "current_message": message,
                "recent_memory": recent_memory,
            },
            metadata={"conversation_id": conversation_id, "source": "news_chat_context_rewrite"},
        )
        try:
            response = await asyncio.wait_for(self.local_agent.chat(request), timeout=12)
        except Exception:
            return message
        if getattr(response, "status", "error") != "ok":
            return message
        rewritten = _clean_contextual_rewrite(getattr(response.message, "content", ""))
        if not rewritten:
            return message
        return rewritten

    def _save_response_turn(
        self,
        response: ChatResponse,
        message: str,
        user_id: str,
        request_topic: str | None,
        request_categories: list[str] | None,
    ) -> str:
        resolved_topic = request_topic
        if not resolved_topic and _response_can_seed_topic(response):
            resolved_topic = response.topic or (
                response.focus_object.text if response.focus_object and response.focus_object.type == "topic" else None
            )
        resolved_categories = response.category_scope or request_categories or []
        turn_id = self.store.save_turn(
            response.conversation_id,
            message,
            response.answer,
            [item.model_dump(mode="json") for item in response.recommendations],
            response.focus_object.model_dump(mode="json") if response.focus_object else None,
            user_id=user_id,
            response=response.model_dump(mode="json"),
            topic=resolved_topic,
            category_scope=resolved_categories,
        )
        response.turn_id = turn_id
        self.store.update_turn_response(turn_id, user_id, response.model_dump(mode="json"))
        return turn_id

    async def _news_search(
        self,
        conversation_id: str,
        message: str,
        topic: str | None = None,
        category_scope: list[str] | None = None,
        use_llm: bool = False,
        user_id: str = "default",
        allow_web_search: bool = False,
        model_key: str = DEFAULT_LOGICAL_MODEL,
    ) -> ChatResponse:
        explicit_query = _explicit_focus_from_message(message)
        search_message = await self._resolve_contextual_search_message(conversation_id, user_id, message, topic)
        query = explicit_query or query_from_message(search_message, topic)
        response_topic = topic or query
        focus_text = query if explicit_query else response_topic
        categories = categories_for_message(search_message, topic, category_scope)
        rule_drift_warning = _topic_drift_warning(topic, query, search_message)
        drift_warning_task = asyncio.create_task(self._llm_topic_drift_warning(topic, query, search_message, rule_drift_warning))
        results = await self.search_service.search(
            query=query,
            category_scope=categories,
            source_scope=None,
            time_range=None,
            max_results=20,
            include_remote=allow_web_search,
        )
        results = _rank_for_chat(_enrich_from_store(self.store, results), message)[:8]
        drift_warning = await drift_warning_task
        if use_llm and self.llm_client.configured and results:
            try:
                history = self._conversation_memory(conversation_id, user_id)
                answer = await self.llm_client.chat(_chat_messages(message, query, categories, results, history))
                answer = _prepend_notice(answer, drift_warning)
                context_relation = "topic_grounded_llm"
            except Exception as exc:
                prefix = _join_notices(drift_warning, f"模型调用失败，已使用本地证据摘要：{exc}")
                answer = _grounded_answer(query, message, results, prefix)
                context_relation = "topic_grounded_fallback"
        else:
            answer = _grounded_answer(query, message, results, drift_warning)
            context_relation = "topic_grounded"
        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            context_relation=context_relation,
            topic=response_topic,
            category_scope=categories or [],
            focus_object=FocusObject(type="topic", text=focus_text),
            required_context_items=["current_topic", "local_news_index", "retrieved_evidence"],
            recommendations=results,
        )

    async def _research_chat(
        self,
        conversation_id: str,
        message: str,
        topic: str | None = None,
        category_scope: list[str] | None = None,
        on_trace: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        user_id: str = "default",
        allow_web_search: bool = False,
        model_key: str = DEFAULT_LOGICAL_MODEL,
    ) -> ChatResponse:
        trace: list[dict[str, Any]] = []
        explicit_query = _explicit_focus_from_message(message)
        search_message = await self._resolve_contextual_search_message(conversation_id, user_id, message, topic)
        query = explicit_query or query_from_message(search_message, topic)
        selected_model = get_model_option(model_key, getattr(self.llm_client, "settings", None))
        response_topic = topic or query
        focus_text = query if explicit_query else response_topic
        categories = categories_for_message(search_message, topic, category_scope)
        rule_drift_warning = _topic_drift_warning(topic, query, search_message)
        drift_warning_task = asyncio.create_task(self._llm_topic_drift_warning(topic, query, search_message, rule_drift_warning))
        time_range = time_range_from_message(search_message)
        search_plan = await self._plan_search_query(search_message, topic, query, time_range, allow_web_search)
        search_query = search_plan.query
        runtime_fallback_trace: list[dict[str, Any]] = []
        if self.cc_runtime and getattr(self.cc_runtime, "configured", False):
            try:
                plan_trace = {
                    "stage": "分析计划",
                    "status": "completed",
                    "message": f"围绕【{query}】制定只读检索与证据核验计划"
                    + (f"，限定近 {time_range.days} 天" if time_range else "")
                    + (f"，分类 {', '.join(categories)}" if categories else "")
                    + ("，允许外部搜索。" if allow_web_search else "，仅使用本地资讯库。"),
                }
                await _add_trace(trace, plan_trace, on_trace)
                await _add_trace(
                    trace,
                    {
                        "stage": "Agent 主控",
                        "status": "running",
                        "message": "正在选择检索词并调用已授权的只读工具。",
                    },
                    on_trace,
                )
                history = _conversation_history_text(
                    self._conversation_memory(conversation_id, user_id),
                    turn_limit=6,
                    question_limit=500,
                    answer_limit=1_500,
                )
                runtime_result = await self.cc_runtime.run(
                    message=message,
                    query=search_query,
                    topic=topic,
                    category_scope=categories,
                    time_range=time_range,
                    history=history,
                    allow_web_search=allow_web_search,
                    logical_model_key=selected_model.key,
                    logical_model_name=selected_model.name,
                    skill_names=[NEWS_CONVERSATION_RESEARCH_SKILL_NAME],
                    max_turns=10,
                    builtin_web_search_limit=4,
                    on_trace=on_trace,
                )
                runtime_declared_results = _runtime_declared_link_results(
                    runtime_result.answer,
                    categories[0] if categories else "all",
                )
                trace.extend(runtime_result.trace)
                runtime_results = _rank_for_chat(
                    _filter_research_results(
                        _filter_by_time(
                            _enrich_from_store(
                                self.store,
                                [*runtime_result.results, *runtime_declared_results],
                            ),
                            time_range,
                        ),
                        search_plan,
                    ),
                    message,
                )[:12]
                if runtime_results:
                    evidence = _evidence_payload(self.store, runtime_results)
                    event_line = await self._event_line(query, categories, runtime_results)
                    drift_warning = await drift_warning_task
                    answer = _prepend_notice(runtime_result.answer, drift_warning)
                    builtin_web_calls = int(runtime_result.provider_metadata.get("builtin_web_calls") or 0)
                    total_tool_calls = len(runtime_result.queries) + builtin_web_calls
                    final_trace = [
                        plan_trace,
                        {
                            "stage": "Agent 主控",
                            "status": "completed",
                            "message": f"自主执行 {total_tool_calls} 次只读检索并组织回答。",
                            "count": total_tool_calls,
                            "logical_model": selected_model.key,
                            "runtime_model": getattr(
                                getattr(self.cc_runtime, "settings", None),
                                "effective_runtime_model",
                                DEFAULT_RUNTIME_MODEL,
                            ),
                        },
                        *runtime_result.trace,
                        {
                            "stage": "证据合并",
                            "status": "completed",
                            "message": f"去重后保留 {len(evidence)} 条可引用证据。",
                            "count": len(evidence),
                        },
                        {"stage": "生成回答", "status": "completed", "message": "Agent 已生成结构化回答。"},
                    ]
                    if event_line and event_line.get("items"):
                        final_trace.insert(
                            -1,
                            {
                                "stage": "事件线",
                                "status": "completed",
                                "message": f"生成 {len(event_line.get('items') or [])} 个时间节点。",
                                "count": len(event_line.get("items") or []),
                            },
                        )
                    await _add_trace(trace, final_trace[1], on_trace)
                    for item in final_trace[2 + len(runtime_result.trace) :]:
                        await _add_trace(trace, item, on_trace)
                    return ChatResponse(
                        conversation_id=conversation_id,
                        answer=answer,
                        markdown=answer,
                        context_relation="research_pipeline_cc_runtime",
                        topic=response_topic,
                        category_scope=categories or [],
                        focus_object=FocusObject(type="topic", text=focus_text),
                        required_context_items=["cc_runtime", "local_news_search", "web_search", "retrieved_evidence", "event_line"],
                        recommendations=runtime_results[:8],
                        research_trace=final_trace,
                        evidence=evidence,
                        expanded_queries=runtime_result.queries[:8],
                        event_line=event_line,
                    )
                runtime_fallback_trace.append(
                    {
                        "stage": "Agent 主控",
                        "status": "fallback",
                        "message": "Agent 未检索到可引用证据，已切换到兼容研究流程。",
                    }
                )
            except Exception as exc:
                self.store.log("cc_runtime_research", "error", query, {"error_type": type(exc).__name__})
                runtime_fallback_trace.append(
                    {
                        "stage": "Agent 主控",
                        "status": "fallback",
                        "message": "Agent 主控暂不可用，已切换到兼容研究流程。",
                    }
                )
        await _add_trace(
            trace,
            {
                "stage": "理解问题",
                "status": "completed",
                "message": f"聚焦【{query}】"
                + (f"，限定近 {time_range.days} 天" if time_range else "")
                + (f"，分类 {', '.join(categories)}" if categories else ""),
            },
            on_trace,
        )
        for item in runtime_fallback_trace:
            await _add_trace(trace, item, on_trace)
        if search_message != message:
            await _add_trace(
                trace,
                {
                    "stage": "上下文改写",
                    "status": "completed",
                    "message": f"已将追问改写为【{search_message[:120]}】。",
                },
                on_trace,
            )
        if search_plan.source == "llm":
            await _add_trace(
                trace,
                {
                    "stage": "查询规划",
                    "status": "completed",
                    "message": f"联网检索式【{search_query}】，核心对象【{search_plan.primary_subject}】。",
                },
                on_trace,
            )
        await _add_trace(trace, {"stage": "本地新闻引擎", "status": "running", "message": "正在检索本地新闻与已抓取正文。"}, on_trace)
        local_results = await self.search_service.search(search_query, categories, None, time_range, max_results=18, include_remote=False)
        local_results = _rank_for_chat(_filter_by_time(_enrich_from_store(self.store, local_results), time_range), message)
        await _add_trace(trace, {"stage": "本地新闻引擎", "status": "completed", "message": f"本地新闻引擎返回 {len(local_results)} 条候选。", "count": len(local_results)}, on_trace)

        ingest_payload: dict[str, Any] | None = None
        if self.native_ingestion and allow_web_search:
            try:
                await _add_trace(trace, {"stage": "新闻源更新", "status": "running", "message": "正在发现新报道并读取正文。"}, on_trace)
                ingest_payload = await self.native_ingestion.ingest(
                    query=search_query,
                    category_scope=categories,
                    source_scope=None,
                    max_results=4,
                    fetch_articles=2,
                    follow_depth=0,
                    follow_limit_per_article=0,
                    max_sources=1,
                    request_timeout_seconds=3.0,
                )
                await _add_trace(
                    trace,
                    {
                        "stage": "新闻源更新",
                        "status": "completed",
                        "message": "已完成新报道发现、正文读取与内容更新。",
                        "count": ingest_payload.get("discovered_count", 0),
                        "details": {
                            "discovered": ingest_payload.get("discovered_count", 0),
                            "fetched": ingest_payload.get("fetched_count", 0),
                            "available": ingest_payload.get("indexed_count", 0),
                        },
                    },
                    on_trace,
                )
            except Exception:
                await _add_trace(trace, {"stage": "新闻源更新", "status": "error", "message": "新闻源更新暂时失败，继续使用已有证据。"}, on_trace)
        elif self.native_ingestion:
            await _add_trace(
                trace,
                {
                    "stage": "新闻源更新",
                    "status": "skipped",
                    "message": "联网回答已关闭，跳过新闻源搜索和正文抓取。",
                },
                on_trace,
            )
        else:
            await _add_trace(trace, {"stage": "新闻源更新", "status": "skipped", "message": "当前未启用新闻源更新能力。"}, on_trace)

        await _add_trace(trace, {"stage": "阅读正文", "status": "running", "message": "正在基于新入库内容重新召回。"}, on_trace)
        refreshed_results = await self.search_service.search(search_query, categories, None, time_range, max_results=24, include_remote=False)
        refreshed_results = _rank_for_chat(_filter_by_time(_enrich_from_store(self.store, refreshed_results), time_range), message)
        await _add_trace(trace, {"stage": "阅读正文", "status": "completed", "message": f"抓取后重新召回 {len(refreshed_results)} 条候选，进入证据合并。", "count": len(refreshed_results)}, on_trace)

        external_results: list[SearchResult] = []
        should_search_external = allow_web_search and self.search_service.external_configured
        if should_search_external:
            try:
                await _add_trace(trace, {"stage": "外部搜索工具", "status": "running", "message": "正在检索外部实时信息。"}, on_trace)
                raw_external_results = await self.search_service.search_external(search_query, categories, None, max_results=8)
                external_results = raw_external_results
                if search_plan.required_terms:
                    external_results = [
                        item
                        for item in raw_external_results
                        if search_result_matches_terms(search_plan.required_terms, item)
                    ]
                await _add_trace(
                    trace,
                    {
                        "stage": "外部搜索工具",
                        "status": "completed",
                        "message": (
                            f"外部搜索工具返回 {len(raw_external_results)} 条，"
                            f"按核心对象保留 {len(external_results)} 条候选。"
                        ),
                        "count": len(external_results),
                    },
                    on_trace,
                )
            except Exception:
                await _add_trace(trace, {"stage": "外部搜索工具", "status": "error", "message": "外部搜索暂时失败，继续使用已有证据。"}, on_trace)

        expanded_queries: list[dict[str, Any]] = []
        expansion_results: list[SearchResult] = []
        if self.deep_dive:
            try:
                await _add_trace(trace, {"stage": "扩展搜索", "status": "running", "message": "正在生成垂直/横向扩展查询。"}, on_trace)
                deep_payload = await self.deep_dive.run(search_query, categories, None, rounds=1, breadth=4, include_remote=False)
                expanded_queries = list(deep_payload.get("expanded_queries") or [])[:6]
                for expansion in expanded_queries[:2]:
                    expansion_query = expansion.get("query")
                    if not expansion_query:
                        continue
                    results = await self.search_service.search(expansion_query, categories, None, time_range, max_results=5, include_remote=False)
                    expansion_results.extend(results)
                if search_plan.required_terms:
                    expansion_results = [
                        item
                        for item in expansion_results
                        if search_result_matches_terms(search_plan.required_terms, item)
                    ]
                else:
                    expansion_results = [
                        item for item in expansion_results if search_result_matches_subject(search_query, item)
                    ]
                expansion_results = _rank_for_chat(_filter_by_time(_enrich_from_store(self.store, expansion_results), time_range), message)
                await _add_trace(
                    trace,
                    {
                        "stage": "扩展搜索",
                        "status": "completed",
                        "message": f"生成 {len(expanded_queries)} 个扩展查询，补充召回 {len(expansion_results)} 条候选。",
                        "count": len(expansion_results),
                    },
                    on_trace,
                )
            except Exception as exc:
                await _add_trace(trace, {"stage": "扩展搜索", "status": "error", "message": f"扩展搜索失败，继续合并已有证据：{exc}"}, on_trace)
        else:
            await _add_trace(trace, {"stage": "扩展搜索", "status": "skipped", "message": "当前服务未注入 deep dive 模块。"}, on_trace)

        merged_results = _merge_results([*external_results, *refreshed_results, *local_results, *expansion_results])
        merged_results = _filter_research_results(_filter_by_time(merged_results, time_range), search_plan)
        merged_results = _rank_for_chat(merged_results, message)[:12]
        evidence = _evidence_payload(self.store, merged_results)
        await _add_trace(trace, {"stage": "证据合并", "status": "completed", "message": f"去重后保留 {len(evidence)} 条可引用证据。", "count": len(evidence)}, on_trace)

        event_line = await self._event_line(query, categories, merged_results)
        if event_line and event_line.get("items"):
            await _add_trace(trace, {"stage": "事件线", "status": "completed", "message": f"生成 {len(event_line.get('items') or [])} 个时间节点。", "count": len(event_line.get("items") or [])}, on_trace)

        drift_warning = await drift_warning_task
        if self.llm_client.configured and evidence:
            try:
                await _add_trace(trace, {"stage": "生成回答", "status": "running", "message": "正在组织 markdown 回答。"}, on_trace)
                history = self._conversation_memory(conversation_id, user_id)
                answer = await self.llm_client.chat(
                    _research_messages(message, query, categories, time_range, evidence, expanded_queries, event_line, trace, history),
                    model_key=selected_model.key,
                )
                answer = _prepend_notice(answer, drift_warning)
                context_relation = "research_pipeline_llm"
            except Exception as exc:
                prefix = _join_notices(drift_warning, f"模型调用失败，已使用本地证据摘要：{exc}")
                answer = _research_fallback_answer(query, evidence, expanded_queries, event_line, prefix)
                context_relation = "research_pipeline_fallback"
        else:
            answer = _research_fallback_answer(query, evidence, expanded_queries, event_line, drift_warning)
            context_relation = "research_pipeline_fallback" if evidence else "research_pipeline_empty"
        await _add_trace(trace, {"stage": "生成回答", "status": "completed", "message": "已生成 markdown 回答。"}, on_trace)

        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            markdown=answer,
            context_relation=context_relation,
            topic=response_topic,
            category_scope=categories or [],
            focus_object=FocusObject(type="topic", text=focus_text),
            required_context_items=["research_pipeline", "source_search_ingest", "retrieved_evidence", "event_line"],
            recommendations=merged_results[:8],
            research_trace=trace,
            evidence=evidence,
            expanded_queries=expanded_queries,
            event_line=event_line,
        )

    async def _llm_topic_drift_warning(
        self,
        topic: str | None,
        query: str,
        message: str,
        rule_notice: str | None,
    ) -> str | None:
        if rule_notice:
            return rule_notice
        if not self.llm_client.configured or not query:
            return rule_notice
        if topic and _looks_like_general_topic_extension(message):
            # Questions about broader effects, relationships, public response,
            # or the same class of issue are valid extensions of the active
            # topic. Do not let a remote classifier add a drift warning after
            # the deterministic contextual rule has already accepted them.
            return None
        focuses = _explicit_focuses_from_message(message)
        if topic and not focuses and _topic_drift_terms(topic).intersection(_topic_drift_terms(query)):
            # Deterministic subject anchors are enough for ordinary follow-ups;
            # avoid an unnecessary remote judgement call in this case.
            return None
        if not topic and len(focuses) < 2:
            return rule_notice
        if topic:
            topic_compact = _compact_topic_text(topic)
            query_compact = _compact_topic_text(query)
            if topic_compact and query_compact and (
                topic_compact == query_compact or topic_compact in query_compact or query_compact in topic_compact
            ):
                return None
        payload = {
            "current_topic": topic or "",
            "normalized_query": query,
            "user_message": message,
            "explicit_focuses": focuses,
            "task": (
                "判断 user_message/normalized_query 是否应当视为 current_topic 的同一主题。"
                "如果没有 current_topic 但 explicit_focuses 有多个，则判断这些热点是否属于同一事件链。"
            ),
        }
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "relation": {"type": "string", "enum": ["same_topic", "related", "weakly_related", "unrelated"]},
                "same_topic": {"type": "boolean"},
                "confidence": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["relation", "same_topic", "confidence", "reason"],
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是新闻主题相关性判定器，只做分类，不回答新闻问题。"
                    "判断标准：同一主题必须共享明确核心主体、同一事件链、同一政策/公司/人物/赛事的连续进展。"
                    "仅同属一个大类（如都是体育、都是中国新闻、都是热点）不算同一主题。"
                    "忽略提问模板词，如围绕热点事件、展开、深挖、追踪、后续观察、告诉我发生了什么。"
                    "如果一个是公共卫生、另一个是消费政策；一个是文物返还、另一个是足球球员表态，应判 unrelated。"
                    "只返回 JSON。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            result = await self.llm_client.structured(messages, "topic_relevance_judgement", schema)
        except Exception:
            return rule_notice
        relation = str(result.get("relation") or "").strip().lower()
        same_topic = result.get("same_topic")
        try:
            confidence = float(result.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if same_topic is False and confidence >= 0.45:
            return MULTI_FOCUS_DRIFT_NOTICE if len(focuses) > 1 else TOPIC_DRIFT_NOTICE
        if relation in {"weakly_related", "unrelated"} and confidence >= 0.45:
            return MULTI_FOCUS_DRIFT_NOTICE if len(focuses) > 1 else TOPIC_DRIFT_NOTICE
        if same_topic is True and relation in {"same_topic", "related"} and confidence >= 0.45:
            return None
        return rule_notice

    async def _plan_search_query(
        self,
        message: str,
        topic: str | None,
        fallback_query: str,
        time_range: TimeRange | None,
        allow_web_search: bool,
    ) -> SearchQueryPlan:
        fallback = SearchQueryPlan(
            query=fallback_query,
            primary_subject=fallback_query,
            required_terms=[],
            keywords=[],
        )
        if not allow_web_search or not self.llm_client.configured:
            return fallback

        system_prompt = (
            "你是搜索查询规划器，不要回答用户的问题。"
            "从用户原话和当前对话主题中识别真正需要检索的具体对象、关系或变化，"
            "不要把宽泛背景、起因或修饰词误当成核心对象。"
            "检索式应把最具体的目标对象放在前面，背景概念放在后面。"
            "只能依据输入改写，不得添加输入中没有依据的具体事实或实体。"
            "只返回一个严格 JSON 对象，字段为："
            '{"query":"适合搜索引擎的简洁检索式",'
            '"primary_subject":"最具体的核心对象或关系",'
            '"required_terms":["每个词都能独立识别核心事件的1到4个短主题词"],'
            '"keywords":["用于扩大召回的2到6个必要概念"]}。'
            "required_terms 必须描述目标对象或事件本身，不能只给地名、人名、行业名、时间词或宽泛背景；"
            "例如查询北京天津防汛，应使用防汛、暴雨、应急响应，而不能只使用北京、天津。"
        )
        user_payload = {
            "message": message,
            "current_topic": topic or "",
            "normalized_query": fallback_query,
            "time_range_days": time_range.days if time_range else None,
        }
        try:
            raw = await self.llm_client.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ]
            )
            payload = _decode_json_object(raw)
            planned_query = _clean_plan_text(payload.get("query"), 180)
            primary_subject = _clean_plan_text(payload.get("primary_subject"), 100)
            required_terms = _clean_plan_terms(payload.get("required_terms"), 4)
            keywords = _clean_plan_terms(payload.get("keywords"), 6)
            if not planned_query or not primary_subject:
                return fallback
            return SearchQueryPlan(
                query=planned_query,
                primary_subject=primary_subject,
                required_terms=required_terms,
                keywords=keywords,
                source="llm",
            )
        except Exception:
            return fallback

    async def _plan_related_queries(
        self,
        query: str,
        categories: list[str] | None,
        user_id: str,
        max_queries: int,
    ) -> tuple[list[dict[str, str]], str]:
        max_queries = _related_query_limit(max_queries)
        fallback = _fallback_related_queries(query, max_queries)
        prompt = (
            "你是个人资讯助手的相关搜索规划器。"
            "你的任务不是回答问题，而是为当前主题生成可以自动搜索的相关检索词。"
            "相关检索词应按主题复杂度动态覆盖 3 到 8 个方向，优先考虑：最新进展、背景脉络、关键主体、影响/争议、规则约束、数据趋势、相似案例、可继续追踪线索。"
            "每个检索词必须标注它和当前主题的联系依据。"
            "不要写硬编码测试词，不要补充没有依据的具体事件。"
            "只返回严格 JSON，不要 markdown，不要解释。"
            "JSON 格式："
            '{"queries":[{"query":"简洁搜索词","relation_type":"latest|background|actor|impact|follow_up|other","reason":"为什么相关"}]}。'
            f"返回 {RELATED_QUERY_MIN} 到 {max_queries} 个。"
            "\n\n"
            f"当前主题：{query}\n"
            f"分类范围：{', '.join(categories or []) or '未限定'}"
        )
        try:
            response = await self.local_agent.chat(
                LocalAgentChatRequest(
                    user_id=user_id,
                    message=prompt,
                    project_context={
                        "feature": "personal_news_agent_related_search",
                        "topic": query,
                        "category_scope": categories or [],
                    },
                    metadata={"purpose": "related_search_query_planning"},
                )
            )
        except Exception:
            return fallback, "fallback"
        if response.status != "ok":
            return fallback, "fallback"
        parsed = _parse_related_queries(response.message.content, max_queries)
        if not parsed:
            return fallback, "fallback"
        return _ensure_related_query_minimum(query, parsed, max_queries), "local_agent"

    async def _article_followup(self, conversation_id: str, message: str, ordinal: int) -> ChatResponse:
        last = self.store.last_turn(conversation_id)
        recommendations = (last or {}).get("recommendations") or []
        if ordinal < 1 or ordinal > len(recommendations):
            return ChatResponse(
                conversation_id=conversation_id,
                answer="上一轮没有对应序号的新闻，请先让我列出一组新闻。",
                context_relation="follow_up",
                focus_object=FocusObject(type="article", source_turn_id=(last or {}).get("id"), ordinal=ordinal),
                required_context_items=["previous_recommendation_list"],
            )
        selected = recommendations[ordinal - 1]
        article_id = selected.get("article_id")
        article = self.store.get_article(article_id) if article_id else None
        if not article:
            return ChatResponse(
                conversation_id=conversation_id,
                answer=f"第{ordinal}条来自外部搜索或尚未入库，当前只能基于标题和摘要说明：{selected.get('title')}。{selected.get('summary', '')}",
                context_relation="follow_up",
                focus_object=FocusObject(type="article", source_turn_id=(last or {}).get("id"), ordinal=ordinal, target_id=article_id),
                required_context_items=["previous_recommendation_list", "article_full_text"],
            )
        related = await self.search_service.search(article["title"], [article["category"]], None, None, max_results=3)
        answer = (
            f"第{ordinal}条是《{article['title']}》。\n"
            f"重要性：它属于{article['category']}板块的近期议题，摘要显示：{article.get('summary') or article.get('content', '')[:160]}\n"
            f"可以继续关注：相关主体、后续政策/产品动作、其他来源是否有交叉验证。"
        )
        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            context_relation="follow_up",
            focus_object=FocusObject(type="article", source_turn_id=(last or {}).get("id"), ordinal=ordinal, target_id=article_id),
            required_context_items=["previous_recommendation_list", "article_full_text", "related_articles"],
            recommendations=related,
        )

    async def _event_line(self, query: str, categories: list[str] | None, results: list[SearchResult]) -> dict[str, Any] | None:
        items = []
        for index, item in enumerate(results[:8], start=1):
            date = _date_text(item.published_at) or "发布时间未知"
            items.append(
                {
                    "id": f"chat_evt_{index}",
                    "date": date,
                    "title": item.title,
                    "summary": item.summary[:180] if item.summary else "",
                    "url": item.url,
                    "stage": "证据",
                    "source_article_ids": [item.article_id] if item.article_id else [],
                }
            )
        if items:
            return {"view_type": "event_line", "items": items, "lanes": []}
        return await _maybe_build_topic_view(self.topic_views, query, categories)


def _decode_json_object(raw: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw or ""):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("search planner did not return a JSON object")


def _skill_answer(title: str, message: str, payload: dict[str, Any]) -> str:
    if payload.get("markdown"):
        return str(payload.get("markdown")).strip()
    if payload.get("verdict"):
        lines = [
            f"## {title}",
            "",
            f"结论：{payload.get('verdict')}（置信度 {payload.get('confidence')}）",
            "",
            payload.get("summary") or message,
        ]
        if payload.get("verdict") == "insufficient":
            reasons = _factcheck_insufficient_reasons(payload)
            if reasons:
                lines.extend(["", "### 为什么仍是证据不足"])
                lines.extend(f"- {item}" for item in reasons)
        for label, key in (("支持证据", "supporting_evidence"), ("反向证据", "contradicting_evidence")):
            values = payload.get(key) or []
            if values:
                lines.extend(["", f"### {label}"])
                for item in values[:6]:
                    if not isinstance(item, dict):
                        continue
                    title = _markdown_link_label(item.get("title") or "未命名证据")
                    url = _markdown_link_url(item.get("url") or "")
                    source = item.get("source_id") or "unknown"
                    if url:
                        lines.append(f"- [{item.get('index')}] [{title}]({url})（{source}）")
                    else:
                        lines.append(f"- [{item.get('index')}] {title}（{source}）")
        evidence = payload.get("evidence") or []
        if evidence:
            lines.extend(["", f"### 检索到的全部证据（{len(evidence)} 条）"])
            for item in evidence[:12]:
                if not isinstance(item, dict):
                    continue
                lines.append(_factcheck_evidence_line(item))
        for label, key in (
            ("缺失证据", "missing_evidence"),
            ("下一步核查", "next_checks"),
            ("来源说明", "source_notes"),
        ):
            values = payload.get(key) or []
            if values:
                lines.extend(["", f"### {label}"])
                lines.extend(f"- {item}" for item in values[:6] if item)
        return "\n".join(lines).strip()
    sections = payload.get("sections") or {}
    if title.startswith("专题报告") and payload.get("report_id") and sections:
        return _report_skill_answer(title, message, payload)
    if sections.get("headline") or sections.get("summary"):
        lines = [f"## {sections.get('headline') or title}", "", sections.get("summary") or message]
        top_stories = sections.get("top_stories") or []
        if top_stories:
            lines.extend(["", "### 今日重点"])
            for index, item in enumerate(top_stories[:5], start=1):
                if not isinstance(item, dict):
                    continue
                story_title = item.get("title") or f"重点 {index}"
                summary = item.get("summary") or ""
                reason = item.get("why_it_matters") or ""
                lines.append(f"{index}. {story_title}")
                if summary:
                    lines.append(f"   {summary}")
                if reason:
                    lines.append(f"   重要性：{reason}")
        for label, key in (("为什么重要", "why_it_matters"), ("可能影响", "impact"), ("接下来关注", "watch_next")):
            values = sections.get(key) or []
            if values:
                lines.extend(["", f"### {label}"])
                lines.extend(f"- {item}" for item in values[:6] if item)
        uncertainty = sections.get("uncertainty")
        if uncertainty:
            lines.extend(["", f"> {uncertainty}"])
        return "\n".join(lines).strip()
    return f"{title}\n\n{message}".strip()


def _skill_context_topic(command: str, payload: dict[str, Any]) -> str | None:
    if command == "/factcheck":
        return payload.get("claim") or None
    if command in {"/report", "/brief", "/map", "/related"}:
        return payload.get("topic") or None
    return None


def _skill_command_has_topic_arg(text: str) -> bool:
    parts = text.split()
    if len(parts) <= 1:
        return False
    index = 1
    while index < len(parts):
        item = parts[index]
        if item.startswith("--"):
            index += 2
            continue
        return True
    return False


def _is_default_brief_topic(topic: str | None) -> bool:
    return _is_topic_placeholder(topic) or str(topic or "").strip() in {"今日简报", "每日摘要", "今日新闻"}


def _is_topic_placeholder(topic: str | None) -> bool:
    return str(topic or "").strip() in {"", "新对话", "当前关注", "今日资讯"}


def _is_general_conversation(message: str, topic: str | None = None) -> bool:
    text = " ".join(str(message or "").strip().split())
    lowered = text.lower().strip("。！!?？ ")
    if not lowered or lowered.startswith("/"):
        return False
    if _is_social_smalltalk(text):
        return True
    if _looks_like_everyday_request(text):
        return True
    news_signals = (
        "新闻",
        "报道",
        "热点",
        "最新",
        "最近",
        "今天",
        "昨日",
        "刚刚",
        "进展",
        "回应",
        "发布",
        "宣布",
        "事件",
        "事故",
        "政策",
        "局势",
        "行情",
        "事实核查",
        "是真是假",
        "消息属实",
        "时间线",
    )
    if any(signal in lowered for signal in news_signals):
        return False
    if topic and is_contextual_followup(text):
        return False
    general_signals = (
        "什么是",
        "是什么意思",
        "怎么理解",
        "解释一下",
        "区别是什么",
        "有什么区别",
        "为什么",
        "如何",
        "怎么做",
        "帮我写",
        "翻译",
        "计算",
        "代码",
        "编程",
        "讲个笑话",
        "聊聊天",
        "陪我聊",
    )
    return any(signal in lowered for signal in general_signals)


def _looks_like_everyday_request(message: str) -> bool:
    text = " ".join(str(message or "").strip().split()).lower()
    everyday_signals = (
        "天气",
        "气温",
        "温度",
        "降雨",
        "会下雨",
        "带伞",
        "穿什么",
        "风力",
        "空气质量",
        "紫外线",
        "航班",
        "航班号",
        "起飞时间",
        "落地时间",
        "飞机延误",
        "航班延误",
        "航班取消",
        "登机口",
        "航站楼",
        "怎么走",
        "路线",
        "导航",
        "怎么去",
        "如何到",
        "到达",
        "乘车",
        "坐地铁",
        "坐公交",
        "步行",
        "自驾",
        "打车",
        "火车",
        "高铁",
        "动车",
        "列车",
        "车次",
        "12306",
        "余票",
    )
    flight_number_status = re.search(
        r"(?<![a-z0-9])[a-z0-9]{2,3}\d{1,4}[a-z]?(?![a-z0-9])",
        text,
    ) and any(
        signal in text for signal in ("延误", "取消", "状态", "起飞", "到达", "落地")
    )
    return bool(any(signal in text for signal in everyday_signals) or flight_number_status)


def _is_social_smalltalk(message: str) -> bool:
    lowered = " ".join(str(message or "").strip().split()).lower().strip("。！!?？ ")
    casual_patterns = (
        r"^(你好|您好|嗨|哈喽|hello|hi|hey)(呀|啊|呢)?([，,、 ]*(你是谁|你叫什么|怎么称呼你|你能做什么))?$",
        r"^(早上好|上午好|下午好|晚上好|晚安)$",
        r"^(谢谢|多谢|感谢|辛苦了|再见|拜拜)(你)?$",
        r"^(你是谁|你叫什么|怎么称呼你|你能做什么|介绍一下你自己)$",
    )
    return any(re.fullmatch(pattern, lowered, flags=re.IGNORECASE) for pattern in casual_patterns)


def _response_can_seed_topic(response: ChatResponse) -> bool:
    relation = response.context_relation or ""
    return (
        relation == "topic_agent_created"
        or relation == "topic_grounded"
        or relation.startswith("topic_grounded_")
        or relation.startswith("research_pipeline")
    )


def _report_skill_answer(title: str, message: str, payload: dict[str, Any]) -> str:
    sections = payload.get("sections") or {}
    topic = payload.get("topic") or title.replace("专题报告：", "").strip()
    sources = payload.get("sources") or []
    timeline = payload.get("timeline") or sections.get("三、关键时间线") or []
    conclusion = sections.get("一句话结论") or sections.get("一、结论摘要") or sections.get("summary") or message
    background = sections.get("发生了什么") or sections.get("二、事件背景")
    key_evidence = sections.get("关键证据") or sections.get("六、不同来源的主要说法") or []
    source_claims = sections.get("各方说法") or sections.get("六、不同来源的主要说法") or []
    importance = sections.get("为什么重要") or _report_importance_points(topic, payload.get("category_scope") or [], sections)
    uncertainty = sections.get("争议与不确定性") or sections.get("八、来源列表与不确定性说明") or sections.get("uncertainty")
    watch_points = sections.get("后续观察点") or sections.get("七、可能影响与后续观察指标") or []
    lines = [
        f"## 专题报告：{_report_title_text(topic)}",
        "",
        "### 一句话结论",
        _report_body_text(conclusion, max_length=420),
        "",
        "### 覆盖范围",
        f"- 主题：{_report_title_text(topic)}",
        f"- 分类：{' / '.join(payload.get('category_scope') or []) or '不限'}",
        f"- 相关证据：{len(sources)} 条",
    ]
    lines.extend(["", "### 发生了什么", _report_body_text(background or "对话内暂未出现足够的事件背景。", max_length=520)])
    lines.extend(["", "### 关键证据"])
    _extend_report_dict_list(lines, key_evidence, empty="对话内暂未出现可引用的关键证据。")
    lines.extend(["", "### 各方说法"])
    _extend_report_dict_list(lines, source_claims, empty="对话内暂未出现不同主体或来源的明确说法。")
    lines.extend(["", "### 为什么重要"])
    _extend_report_string_list(lines, importance, empty="对话内暂未出现足够信息判断重要性。")
    lines.extend(["", "### 争议与不确定性"])
    _extend_report_string_list(lines, uncertainty, empty="对话内暂未出现明确争议或不确定性说明。")
    lines.extend(["", "### 后续观察点"])
    _extend_report_string_list(lines, watch_points, empty="对话内暂未出现后续观察点。")
    lines.extend(["", "### 时间线"])
    _extend_report_timeline(lines, timeline)
    lines.extend(["", "### 证据来源列表"])
    _extend_report_sources(lines, sources)
    return "\n".join(str(line) for line in lines if line is not None).strip()


def _extend_report_dict_list(lines: list[str], values: Any, empty: str) -> None:
    if not isinstance(values, list) or not values:
        lines.append(f"- {empty}")
        return
    appended = False
    for item in values[:8]:
        if isinstance(item, dict):
            summary = _report_body_text(item.get("summary") or "", max_length=360)
            title = _report_title_text(item.get("title") or "未命名来源")
            source = item.get("source_id") or item.get("source") or "unknown"
            lines.append(f"- {title}（{source}）{f'：{summary}' if summary else ''}")
            appended = True
        elif item:
            lines.append(f"- {_report_body_text(item, max_length=360)}")
            appended = True
    if not appended:
        lines.append(f"- {empty}")


def _extend_report_string_list(lines: list[str], values: Any, empty: str) -> None:
    if isinstance(values, str):
        text = _report_body_text(values, max_length=360)
        lines.append(f"- {text}" if text else f"- {empty}")
        return
    if not isinstance(values, list) or not values:
        lines.append(f"- {empty}")
        return
    appended = False
    for item in values[:8]:
        if item and not _report_fragment_text(item):
            lines.append(f"- {_report_body_text(item, max_length=360)}")
            appended = True
    if not appended:
        lines.append(f"- {empty}")


def _extend_report_timeline(lines: list[str], timeline: Any) -> None:
    if not isinstance(timeline, list) or not timeline:
        lines.append("- 时间未知：对话内暂未出现足够内容形成时间线")
        return
    appended = False
    for item in timeline[:10]:
        if not isinstance(item, dict):
            continue
        date = item.get("date") or "时间未知"
        event = _report_title_text(item.get("event") or item.get("title") or "")
        if event:
            lines.append(f"- {date}：{event}")
            appended = True
    if not appended:
        lines.append("- 时间未知：对话内暂未出现足够内容形成时间线")


def _extend_report_sources(lines: list[str], sources: Any) -> None:
    if not isinstance(sources, list) or not sources:
        lines.append("- 对话内暂未出现可列出的证据来源。")
        return
    appended = False
    for index, item in enumerate(sources[:12], start=1):
        if not isinstance(item, dict):
            continue
        source = item.get("source_id") or "unknown"
        source_title = _markdown_link_label(_report_title_text(item.get("title") or "未命名来源"))
        url = item.get("url") or ""
        title_part = f"[{source_title}]({_markdown_link_url(url)})" if _markdown_link_url(url) else source_title
        lines.append(f"- [{index}] {title_part}（{source}）")
        appended = True
    if not appended:
        lines.append("- 对话内暂未出现可列出的证据来源。")


def _report_importance_points(topic: str, categories: list[str], sections: dict[str, Any]) -> list[str]:
    points = sections.get("五、主要争议点/看点") or []
    if points:
        readable_points = [item for item in points if item and not _report_fragment_text(item)]
        if readable_points:
            return [f"对“{topic}”的判断主要取决于：{item}" for item in readable_points[:3]]
    if categories:
        return [f"该主题涉及 {' / '.join(categories)} 板块，后续变化可能影响相关主体和公众判断。"]
    return [f"该主题后续走向可能改变对“{topic}”的判断，需要继续关注证据是否补齐。"]


def _report_fragment_text(value: Any) -> bool:
    text = " ".join(str(value or "").split()).strip(" -—_·|：:，,。")
    if not text:
        return True
    lowered = text.lower()
    if lowered in {"36", "-36", "36氪", "kr36", "com", "同花顺", "亿元", "万元", "2025", "2026", "10"}:
        return True
    if text.isdigit() or re.fullmatch(r"20\d{2}", text):
        return True
    has_cjk = any("\u4e00" <= char <= "\u9fff" for char in text)
    return has_cjk and len(text) <= 4


def _report_title_text(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text.rstrip("…")


def _report_body_text(value: Any, max_length: int = 360) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    was_previously_truncated = text.endswith("…") or text.endswith("...")
    if len(text) <= max_length and not was_previously_truncated:
        return text
    text = text.rstrip(".…").rstrip()
    excerpt = text[:max_length].rstrip("，,。；;：:、 ")
    return f"{excerpt}（已截断，仅展示对话内摘录）"


def _factcheck_insufficient_reasons(payload: dict[str, Any]) -> list[str]:
    reasons = []
    if not payload.get("supporting_evidence") and not payload.get("contradicting_evidence"):
        reasons.append("裁判没有从检索结果中确认可直接支持或直接反驳该说法的证据。")
    missing = payload.get("missing_evidence") or []
    if missing:
        reasons.append("仍缺少：" + "、".join(str(item) for item in missing[:3] if item) + "。")
    if payload.get("agent_source") == "fallback":
        reasons.append("本轮未获得事实核查裁判的明确结构化判定，因此按保守规则返回 insufficient。")
    check_query = payload.get("check_query")
    if check_query:
        reasons.append(f"本轮已按「{check_query}」补充搜索，但补到的是相关材料，不等于已验证的原始证据。")
    return [item for item in reasons if item]


def _factcheck_evidence_line(item: dict[str, Any]) -> str:
    index = item.get("index") or "?"
    title = item.get("title") or "未命名证据"
    source = item.get("source_id") or "unknown"
    origin = item.get("origin") or "unknown"
    date = item.get("published_at") or "时间未知"
    summary = item.get("summary") or item.get("content_excerpt") or ""
    url = item.get("url") or ""
    title = _markdown_link_label(title)
    link_url = _markdown_link_url(url)
    title_part = f"[{title}]({link_url})" if link_url else title
    head = f"- [{index}] {title_part}（{source}，{origin}，{date}）"
    if summary:
        head += f"：{summary[:180]}"
    return head


def _explicit_focus_from_message(message: str) -> str:
    focuses = _explicit_focuses_from_message(message)
    if not focuses:
        return ""
    if len(focuses) == 1:
        return focuses[0]
    return "多热点：" + "；".join(focuses[:4])


def _explicit_focuses_from_message(message: str) -> list[str]:
    text = str(message or "")
    patterns = (
        r"围绕热点事件[“\"《](.+?)[”\"》]",
        r"基于资讯[“\"《](.+?)[”\"》]",
    )
    focuses: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for value in re.findall(pattern, text):
            cleaned = _clean_focus_title(value)
            key = _compact_topic_text(cleaned)
            if cleaned and key not in seen:
                focuses.append(cleaned)
                seen.add(key)
    return focuses


def _hot_event_focus_from_message(message: str) -> str:
    return _explicit_focus_from_message(message)


def _clean_focus_title(value: str) -> str:
    text = " ".join(str(value or "").split()).strip(" \t\r\n:：,，。；;!?！？")
    if not text:
        return ""
    if "相关热点：" in text:
        text = text.rsplit("相关热点：", 1)[-1].strip()
    if "相关热点:" in text:
        text = text.rsplit("相关热点:", 1)[-1].strip()
    text = re.sub(r"^(围绕)?热点事件", "", text).strip(" “\"《》")
    text = _strip_topic_template_phrases(text)
    for separator in ("--", "——", " - ", "-"):
        if separator not in text:
            continue
        head, tail = text.rsplit(separator, 1)
        if tail.strip() in {"人民网", "中新网", "新华网", "央视网", "中国新闻网", "中国共产党新闻网"}:
            text = head.strip()
            break
    return text[:120] or ""


def _clean_plan_text(value: Any, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()[:max_length]


def _clean_plan_terms(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    terms: list[str] = []
    for item in value:
        term = _clean_plan_text(item, 40)
        if len(term) < 2 or term in terms:
            continue
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def _fallback_related_queries(query: str, max_queries: int) -> list[dict[str, str]]:
    templates = [
        ("{query} 最新进展", "latest", "同一主题的近期变化。"),
        ("{query} 背景 脉络", "background", "补充主题的来龙去脉。"),
        ("{query} 关键主体", "actor", "寻找关联人物、机构、公司或地区。"),
        ("{query} 影响 争议", "impact", "关注影响面和争议点。"),
        ("{query} 政策 规则 监管", "impact", "查看相关规则、监管要求或制度约束。"),
        ("{query} 数据 趋势 规模", "background", "用数据和趋势补充判断依据。"),
        ("{query} 类似案例 对比", "other", "寻找可以互相参照的相似案例。"),
        ("{query} 后续 追踪", "follow_up", "发现可持续跟踪的线索。"),
    ]
    cleaned = " ".join((query or "").split()).strip() or "当前主题"
    max_queries = _related_query_limit(max_queries)
    return [
        {
            "query": template.format(query=cleaned),
            "relation_type": relation_type,
            "relation_label": _related_relation_label(relation_type),
            "reason": reason,
        }
        for template, relation_type, reason in templates[:max_queries]
    ]


def _related_base_query(planned_query: str, raw_query: str) -> str:
    cleaned = " ".join((raw_query or planned_query or "").split()).strip()
    for pattern in (
        r"围绕热点事件[“\"]([^”\"]{2,180})[”\"]",
        r"围绕[“\"]([^”\"]{2,180})[”\"](?:展开|做|进行)",
    ):
        matches = re.findall(pattern, cleaned)
        if matches:
            return _clean_related_topic(matches[-1])
    return _clean_related_topic(planned_query)


def _related_context_query(topic: str | None, requested_focus: str) -> str:
    active_topic = _clean_related_topic(topic or "") if topic else ""
    focus = _clean_related_topic(requested_focus)
    if focus in {"当前关注", "当前主题"}:
        focus = ""
    if not active_topic:
        return focus or "当前主题"
    if not focus:
        return active_topic
    compact_topic = _compact_topic_text(active_topic)
    compact_focus = _compact_topic_text(focus)
    if compact_focus and compact_focus in compact_topic:
        return active_topic
    return f"{active_topic} 中的 {focus}"[:180]


def _related_runtime_failure_message(exc: Exception) -> str:
    detail = str(exc or "").casefold()
    if "maximum number of turns" in detail:
        return "外部研究轮次达到本轮上限，已切换到带当前话题约束的兼容检索。"
    if "timed out" in detail or "timeout" in detail:
        return "外部研究未能在本轮时限内完成，已切换到带当前话题约束的兼容检索。"
    return "Agent 研究链路本轮未完成，已切换到带当前话题约束的兼容检索。"


def _runtime_related_queries(
    base_query: str,
    runtime_queries: list[dict[str, Any]],
    max_queries: int,
) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in runtime_queries:
        query = _clean_plan_text(item.get("query"), 120)
        key = query.casefold()
        if not query or key in seen:
            continue
        relation_type = _infer_related_relation_type(query, "")
        origin = item.get("origin") or "local"
        queries.append(
            {
                "query": query,
                "relation_type": relation_type,
                "relation_label": _related_relation_label(relation_type),
                "reason": "用于身份消歧和关系核验。" if origin == "local" else "用于核对当前外部信息。",
                "origin": origin,
                "result_count": int(item.get("result_count") or 0),
                "result_urls": list(item.get("result_urls") or [])[:12],
            }
        )
        seen.add(key)
        if len(queries) >= max_queries:
            break
    if queries:
        return queries
    return [
        {
            "query": base_query,
            "relation_type": "related",
            "relation_label": "与当前主题的关系",
            "reason": "围绕当前对话主题解释用户指定焦点。",
        }
    ]


def _related_groups_from_runtime(
    related_queries: list[dict[str, Any]],
    results: list[SearchResult],
) -> list[dict[str, Any]]:
    grouped: list[dict[str, Any]] = []
    for item in related_queries[:5]:
        query = item.get("query") or ""
        recorded_urls = {
            canonicalize_url(str(url or ""))
            for url in item.get("result_urls") or []
            if canonicalize_url(str(url or ""))
        }
        recorded_results = [
            result
            for result in results
            if canonicalize_url(str(result.url or "")) in recorded_urls
        ]
        ranked = _rank_for_chat(recorded_results or results, query)[:4]
        grouped.append(
            {
                "query": query,
                "reason": item.get("reason") or "",
                "relation_type": item.get("relation_type") or "other",
                "relation_label": item.get("relation_label") or _related_relation_label(item.get("relation_type") or "other"),
                "count": len(ranked),
                "items": ranked,
            }
        )
    return grouped


def _filter_related_runtime_results(
    results: list[SearchResult],
    topic: str | None,
    requested_focus: str,
) -> list[SearchResult]:
    focus_terms = _related_identity_terms(requested_focus)
    topic_terms = _related_identity_terms(topic or "")
    if not focus_terms or not topic_terms:
        return results
    filtered: list[SearchResult] = []
    for item in results:
        if item.origin == "external":
            filtered.append(item)
            continue
        text = f"{item.title or ''} {item.summary or ''}"
        if any(_related_term_in_text(term, text) for term in focus_terms) and any(
            _related_term_in_text(term, text) for term in topic_terms
        ):
            filtered.append(item)
    return filtered


def _filter_research_results(
    results: list[SearchResult],
    search_plan: SearchQueryPlan,
) -> list[SearchResult]:
    """Keep only evidence that still names the planned subject or event.

    Agent runs may issue several broad discovery queries. Their raw union is a
    candidate pool, not a user-facing evidence list, so every path passes this
    final relevance gate before citations and recommendations are rendered.
    """
    if search_plan.required_terms:
        return [item for item in results if search_result_matches_terms(search_plan.required_terms, item)]
    subject = search_plan.primary_subject or search_plan.query
    return [item for item in results if search_result_matches_subject(subject, item)]


def _related_identity_terms(value: str) -> list[str]:
    text = " ".join(str(value or "").split()).strip().lower()
    latin = re.findall(r"[a-z][a-z0-9+#._-]{1,}", text)
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    return [*latin, *cjk][:6]


def _related_term_in_text(term: str, value: str) -> bool:
    if re.fullmatch(r"[a-z0-9+#._-]+", term):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", value, flags=re.IGNORECASE))
    return term in value


def _clean_related_topic(value: str) -> str:
    topic = " ".join((value or "").split()).strip()
    if "相关热点：" in topic:
        topic = topic.rsplit("相关热点：", 1)[-1].strip()
    if "相关热点:" in topic:
        topic = topic.rsplit("相关热点:", 1)[-1].strip()
    if "--" in topic:
        head, tail = topic.rsplit("--", 1)
        if any(marker in tail for marker in ("网", "频道", "党建", "新闻")):
            topic = head.strip()
    return topic[:120] or "当前主题"


def _related_query_limit(max_queries: int) -> int:
    try:
        value = int(max_queries)
    except (TypeError, ValueError):
        value = RELATED_QUERY_MAX
    return max(RELATED_QUERY_MIN, min(RELATED_QUERY_MAX, value))


def _parse_related_queries(raw: str, max_queries: int) -> list[dict[str, str]]:
    max_queries = _related_query_limit(max_queries)
    try:
        payload = _decode_json_object(raw)
    except Exception:
        return []
    source = payload.get("queries")
    if not isinstance(source, list):
        source = [payload] if payload.get("query") else []
    parsed: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in source:
        if not isinstance(item, dict):
            continue
        query = _clean_plan_text(item.get("query"), 120)
        relation_type = _clean_plan_text(item.get("relation_type") or item.get("dimension"), 40)
        reason = _clean_plan_text(item.get("reason") or item.get("rationale"), 160)
        relation_label = _clean_plan_text(item.get("relation_label") or item.get("label"), 40)
        key = query.casefold()
        if len(query) < 2 or key in seen:
            continue
        if not relation_type:
            relation_type = _infer_related_relation_type(query, reason)
        parsed.append(
            {
                "query": query,
                "relation_type": relation_type,
                "relation_label": relation_label or _related_relation_label(relation_type),
                "reason": reason,
            }
        )
        seen.add(key)
        if len(parsed) >= max_queries:
            break
    return parsed


def _ensure_related_query_minimum(query: str, planned: list[dict[str, str]], max_queries: int) -> list[dict[str, str]]:
    max_queries = _related_query_limit(max_queries)
    min_queries = min(RELATED_QUERY_MIN, max_queries)
    if len(planned) >= min_queries:
        return planned[:max_queries]
    seen = {(item.get("query") or "").casefold() for item in planned}
    supplemented = list(planned)
    for item in _fallback_related_queries(query, max_queries):
        key = (item.get("query") or "").casefold()
        if not key or key in seen:
            continue
        supplemented.append(item)
        seen.add(key)
        if len(supplemented) >= min_queries:
            break
    return supplemented[:max_queries]


def _infer_related_relation_type(query: str, reason: str) -> str:
    text = f"{query} {reason}"
    if any(term in text for term in ["最新", "近期", "变化", "进展"]):
        return "latest"
    if any(term in text for term in ["背景", "脉络", "历史", "来龙去脉"]):
        return "background"
    if any(term in text for term in ["主体", "人物", "机构", "公司", "地区"]):
        return "actor"
    if any(term in text for term in ["影响", "争议", "风险", "机会", "市场", "政策"]):
        return "impact"
    if any(term in text for term in ["后续", "追踪", "持续", "线索"]):
        return "follow_up"
    return "other"


def _related_relation_label(relation_type: str) -> str:
    labels = {
        "latest": "最新进展",
        "background": "背景脉络",
        "actor": "关键主体",
        "impact": "影响/争议",
        "follow_up": "后续追踪",
        "other": "语义相关",
    }
    return labels.get(relation_type, labels["other"])


async def _add_trace(
    trace: list[dict[str, Any]],
    item: dict[str, Any],
    on_trace: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> None:
    stage = str(item.get("stage") or "")
    status = str(item.get("status") or "completed")
    replace_index = next(
        (
            index
            for index in range(len(trace) - 1, -1, -1)
            if trace[index].get("stage") == stage and trace[index].get("status") == "running"
        ),
        None,
    )
    if status != "running" and replace_index is not None:
        trace[replace_index] = item
    elif status == "running" and any(
        existing.get("stage") == stage and existing.get("status") != "running" for existing in trace
    ):
        # An older running snapshot must not supersede a terminal state.
        pass
    else:
        trace.append(item)
    if on_trace:
        await on_trace(item)


def _grounded_answer(query: str, message: str, results: list[SearchResult], prefix: str | None = None) -> str:
    if not results:
        lead = f"{prefix}\n\n" if prefix else ""
        return f"{lead}我现在没有在本地新闻引擎里找到【{query}】的可靠证据。可以先更新新闻源，再继续问我。"
    top = results[:5]
    dates = sorted({_date_text(item.published_at) for item in top if _date_text(item.published_at)})
    sources = "、".join(sorted({item.source_id for item in top}))
    bullets = []
    for item in top[:4]:
        summary = (item.summary or "").strip()
        detail = summary[:90] + ("…" if len(summary) > 90 else "")
        date = _date_text(item.published_at) or "发布时间未知"
        bullets.append(f"- {item.title}（{item.source_id}，{date}）：{detail or '暂无摘要'}")
    lead = prefix + "\n\n" if prefix else ""
    return (
        f"{lead}围绕【{query}】，我现在基于 {len(results)} 条本地证据回答。\n"
        f"时间覆盖：{dates[0] + ' 至 ' + dates[-1] if dates else '部分来源发布时间未知'}；来源：{sources or '本地新闻引擎'}。\n\n"
        "当前主要变化：\n"
        + "\n".join(bullets)
        + "\n\n可以继续追问：赛事成绩线、商业/上市传闻线、舆论争议线，或让我把它升级为持续跟踪专题。"
    )


def _topic_drift_warning(topic: str | None, query: str, message: str) -> str | None:
    focuses = _explicit_focuses_from_message(message)
    if len(focuses) > 1 and _focuses_are_weakly_related(focuses):
        return MULTI_FOCUS_DRIFT_NOTICE
    if not topic or not query:
        return None
    has_explicit_focus = bool(focuses)
    if _compact_topic_text(topic) == _compact_topic_text(query):
        return None
    if _compact_topic_text(topic) in _compact_topic_text(query) or _compact_topic_text(query) in _compact_topic_text(topic):
        return None
    if not has_explicit_focus and is_contextual_followup(message):
        return None
    if not has_explicit_focus and _looks_like_general_topic_extension(message):
        return None
    if _has_new_latin_subject(topic, query):
        return TOPIC_DRIFT_NOTICE
    topic_terms = _topic_drift_terms(topic)
    query_terms = _topic_drift_terms(query)
    if topic_terms and query_terms and topic_terms.intersection(query_terms):
        return None
    if not _has_strong_new_subject(query) and len(query_terms) < 2:
        return None
    return TOPIC_DRIFT_NOTICE


def _focuses_are_weakly_related(focuses: list[str]) -> bool:
    if len(focuses) < 2:
        return False
    base_terms = _topic_drift_terms(focuses[0])
    for focus in focuses[1:]:
        terms = _topic_drift_terms(focus)
        if base_terms and terms and base_terms.intersection(terms):
            continue
        return True
    return False


def _has_new_latin_subject(topic: str, query: str) -> bool:
    compact_topic = _compact_topic_text(topic)
    for term in re.findall(r"[a-z][a-z0-9+#._-]{2,}", _compact_topic_text(query)):
        if term not in compact_topic and term not in _TOPIC_DRIFT_STOP_TERMS:
            return True
    return False


def _prepend_notice(answer: str, notice: str | None) -> str:
    return f"> {notice}\n\n{answer}" if notice else answer


def _join_notices(*items: str | None) -> str | None:
    notices = [item for item in items if item]
    return "\n".join(notices) if notices else None


def _topic_drift_terms(value: str) -> set[str]:
    compact = _compact_topic_text(value)
    terms = set(re.findall(r"[a-z0-9+#._-]{2,}|[\u4e00-\u9fff]{2,}", compact))
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", compact)
    # Keep short CJK subject anchors (for example “游戏”) so natural
    # follow-up wording can remain in the same conversation topic even when
    # the full sentence is rephrased.
    for size in (6, 5, 4, 3, 2):
        if len(cjk) >= size:
            terms.update(cjk[index : index + size] for index in range(0, len(cjk) - size + 1))
    return {term for term in terms if term not in _TOPIC_DRIFT_STOP_TERMS}


def _has_strong_new_subject(value: str) -> bool:
    compact = _compact_topic_text(value)
    if re.search(r"[a-z][a-z0-9+#._-]*[\u4e00-\u9fff]{2,}", compact):
        return True
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", compact)
    cjk = re.sub(r"(是什么|存在吗|有哪些|怎么样|为什么|的影响|影响|走向|风评|前景|情况)$", "", cjk)
    if len(cjk) >= 4 and cjk not in _TOPIC_DRIFT_GENERIC_SUBJECTS:
        return True
    return False


def _looks_like_general_topic_extension(message: str) -> bool:
    compact = _compact_topic_text(message)
    markers = (
        "这种", "这类", "这件事", "上述", "一般", "影响", "风评", "舆论",
        "私生活", "后果", "怎么看", "有什么关系",
    )
    return any(marker in compact for marker in markers)


def _compact_topic_text(value: str) -> str:
    compact = re.sub(r"\s+", "", str(value or "").lower())
    compact = re.sub(r"^\d+[:：]?", "", compact)
    for ignored in sorted(TOPIC_TEMPLATE_PHRASES, key=len, reverse=True):
        compact = compact.replace(ignored, "")
    compact = re.sub(r"\d*相关热点[:：]?", "", compact)
    compact = re.sub(r"^\d+[:：]?", "", compact)
    for source in ("-中新网", "-新华网", "-人民网", "-央视网", "-中国新闻网", "-中国共产党新闻网"):
        compact = compact.replace(source, "")
    compact = re.sub(r"(中新网|新华网|人民网|央视网|中国新闻网|中国共产党新闻网)$", "", compact)
    compact = re.sub(r"[，。、；;:：!?！？“”\"《》「」]+", "", compact)
    compact = re.sub(r"^\d+(?=[\u4e00-\u9fff])", "", compact)
    compact = compact.strip("和")
    return compact


def _strip_topic_template_phrases(value: str) -> str:
    text = str(value or "")
    for phrase in sorted(TOPIC_TEMPLATE_PHRASES, key=len, reverse=True):
        text = text.replace(phrase, "")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n:：,，。；;!?！？“”\"《》")


_TOPIC_DRIFT_STOP_TERMS = {
    "新闻",
    "相关",
    "热点",
    "资讯",
    "继续",
    "深挖",
    "跟踪",
    "追踪",
    "是否",
    "出现",
    "后续",
    "新进展",
    "怎么样",
    "为什么",
    "有哪些",
    "是什么",
    # Generic nouns that otherwise create false overlap when short CJK
    # anchors are used for natural-language follow-ups.
    "公司",
    "企业",
    "发展",
}


_TOPIC_DRIFT_GENERIC_SUBJECTS = {
    "明星",
    "私生活",
    "网络风评",
    "舆论",
    "公众讨论",
}


def _related_search_answer(
    query: str,
    grouped: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    planner_source: str,
) -> str:
    planner_label = "local agent" if planner_source == "local_agent" else "fallback"
    lines = [
        f"我围绕【{query}】自动生成了 {len(grouped)} 组相关搜索，并完成检索。",
        f"规划来源：{planner_label}；合并证据：{len(evidence)} 条。",
        "",
        "## 相关思维导图",
        f"- 中心主题：{query}",
    ]
    for index, group in enumerate(grouped, start=1):
        relation_label = group.get("relation_label") or _related_relation_label(group.get("relation_type") or "other")
        reason = f"：{group['reason']}" if group.get("reason") else ""
        lines.append(f"- 分支 {index}｜{group.get('query', '')}（依据：{relation_label}{reason}）")
        items = group.get("items") or []
        for item_index, item in enumerate(items[:3], start=1):
            date = _date_text(item.published_at) or "日期未知"
            summary = " ".join((item.summary or "").split()).strip()
            excerpt = summary[:80] + ("…" if len(summary) > 80 else "")
            lines.append(f"- 小点 {index}.{item_index}｜{item.title}（{item.source_id}，{date}）：{excerpt or '暂无摘要'}")
    lines.append("")
    lines.append("## 证据索引")
    if not evidence:
        lines.append("暂时没有召回可引用证据。可以打开外部搜索或先更新一次新闻源，再重新执行 /related。")
        return "\n".join(lines)
    for item in evidence:
        date = item.get("published_at") or "日期未知"
        summary = " ".join((item.get("summary") or "").split()).strip()
        excerpt = summary[:110] + ("…" if len(summary) > 110 else "")
        label = _markdown_link_label(f"证据 {item.get('index')}｜{item.get('title') or '未命名证据'}")
        url = _markdown_link_url(item.get("url") or "")
        if url:
            lines.append(f"- [{label}]({url})（{item.get('source_id')}，{date}）：{excerpt or '暂无摘要'}")
        else:
            lines.append(f"- {label}（{item.get('source_id')}，{date}）：{excerpt or '暂无摘要'}")
    lines.append("")
    lines.append("## 下一步")
    lines.append("可以从上面的某一组继续深挖，或把其中一个相关方向新增为关注。")
    return "\n".join(lines)


def _markdown_link_label(value: Any) -> str:
    return str(value or "").replace("[", "【").replace("]", "】").replace("\n", " ").strip()


def _markdown_link_url(value: Any) -> str:
    url = str(value or "").strip()
    return url.replace(")", "%29").replace(" ", "%20") if url.startswith(("http://", "https://")) else ""


def _runtime_declared_link_results(answer: str, category: str) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen: set[str] = set()
    for label, raw_url in re.findall(r"\[([^\]\n]{1,240})\]\((https?://[^\s)]+)\)", answer or ""):
        url = raw_url.strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or url in seen:
            continue
        seen.add(url)
        title = re.sub(r"\s+", " ", label).strip() or parsed.netloc
        results.append(
            SearchResult(
                source_id=parsed.netloc.lower(),
                title=title[:240],
                url=url,
                summary="由外部搜索工具返回；可打开原始页面核对全文。",
                category=category or "all",
                published_at=None,
                score=0.72,
                origin="external",
            )
        )
        if len(results) >= 12:
            break
    return results


def _related_mind_map_payload(
    query: str,
    grouped: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    planner_source: str,
    *,
    active_topic: str | None = None,
    requested_focus: str | None = None,
    conclusion: str | None = None,
    runtime_queries: list[dict[str, Any]] | None = None,
    builtin_web_calls: int = 0,
) -> dict[str, Any]:
    evidence_index_by_key = {
        item.get("article_id") or item.get("url"): item.get("index")
        for item in evidence
        if item.get("article_id") or item.get("url")
    }
    branches = []
    for group in grouped:
        relation_type = group.get("relation_type") or "other"
        relation_label = group.get("relation_label") or _related_relation_label(relation_type)
        edge_reason = group.get("reason") or f"按「{relation_label}」方向扩展当前主题。"
        points = []
        for item in (group.get("items") or [])[:4]:
            key = item.article_id or item.url
            summary = " ".join((item.summary or "").split()).strip()
            points.append(
                {
                    "title": item.title,
                    "summary": summary[:140] + ("…" if len(summary) > 140 else ""),
                    "source_id": item.source_id,
                    "date": _date_text(item.published_at),
                    "url": item.url,
                    "evidence_index": evidence_index_by_key.get(key),
                    "connection_reason": f"检索命中「{relation_label}」方向",
                }
            )
        branches.append(
            {
                "title": group.get("query") or "",
                "reason": group.get("reason") or "",
                "relation_type": relation_type,
                "relation_label": relation_label,
                "edge_reason": edge_reason,
                "count": group.get("count") or len(points),
                "points": points,
                "evidence_indices": [point["evidence_index"] for point in points if point.get("evidence_index")],
            }
        )
    steps: list[dict[str, Any]] = [
        {
            "kind": "context",
            "label": "语境锚点",
            "title": active_topic or query,
            "detail": f"在当前话题中识别并核对「{requested_focus or query}」。",
            "status": "completed",
            "evidence_indices": [],
        },
        {
            "kind": "resolution",
            "label": "对象消歧",
            "title": requested_focus or query,
            "detail": f"将检索对象解析为「{query}」，避免脱离上下文匹配同名对象。",
            "status": "completed",
            "evidence_indices": [],
        },
    ]
    query_records = runtime_queries or []
    for index, branch in enumerate(branches[:5]):
        record = query_records[index] if index < len(query_records) else {}
        origin = str(record.get("origin") or "local")
        evidence_indices = branch.get("evidence_indices") or []
        steps.append(
            {
                "kind": "web_search" if origin == "web" else "local_search",
                "label": "外部证据核对" if origin == "web" else "本地新闻检索",
                "title": branch.get("title") or query,
                "detail": branch.get("edge_reason") or "围绕已消歧对象检索可引用报道。",
                "status": "completed" if evidence_indices else "limited",
                "result_count": int(record.get("result_count") or branch.get("count") or 0),
                "evidence_indices": evidence_indices,
                "points": branch.get("points") or [],
            }
        )
    if builtin_web_calls and not any(step.get("kind") == "web_search" for step in steps):
        steps.append(
            {
                "kind": "web_search",
                "label": "外部证据核对",
                "title": "实时网页交叉核验",
                "detail": f"外部搜索工具完成 {builtin_web_calls} 次检索，核对身份、时间与当前关系。",
                "status": "completed",
                "result_count": builtin_web_calls,
                "evidence_indices": [item.get("index") for item in evidence if item.get("origin") == "external"],
                "points": [item for item in evidence if item.get("origin") == "external"][:5],
            }
        )
    steps.append(
        {
            "kind": "conclusion",
            "label": "关系结论",
            "title": "形成面向当前话题的回答",
            "detail": conclusion or "根据已召回证据说明对象与当前话题的具体关系。",
            "status": "completed" if evidence else "limited",
            "evidence_indices": [item.get("index") for item in evidence[:6] if item.get("index")],
            "points": evidence[:6],
        }
    )
    return {
        "type": "related_research_path_v2",
        "topic": query,
        "active_topic": active_topic or query,
        "requested_focus": requested_focus or query,
        "planner_source": planner_source,
        "evidence_count": len(evidence),
        "steps": steps,
        "branches": branches,
    }


def _related_conclusion(answer: str) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", str(answer or ""))
    paragraphs = [
        re.sub(r"\s+", " ", item).strip(" -#")
        for item in re.split(r"\n\s*\n", text)
        if re.sub(r"\s+", " ", item).strip(" -#")
    ]
    for paragraph in paragraphs:
        if paragraph.startswith(("来源", "证据", "下一步")):
            continue
        return paragraph[:220] + ("…" if len(paragraph) > 220 else "")
    return ""


def _enrich_from_store(store: NewsStore, results: list[SearchResult]) -> list[SearchResult]:
    enriched = []
    for item in results:
        if not item.article_id:
            enriched.append(item)
            continue
        row = store.get_article(item.article_id)
        if not row:
            enriched.append(item)
            continue
        enriched.append(
            item.model_copy(
                update={
                    "title": row.get("title") or item.title,
                    "summary": row.get("summary") or item.summary,
                    "category": row.get("category") or item.category,
                    "published_at": _date_sort_value(row.get("published_at")),
                }
            )
        )
    return enriched


def _filter_by_time(results: list[SearchResult], time_range: TimeRange | None) -> list[SearchResult]:
    if not time_range:
        return results
    cutoff = datetime.now(timezone.utc) - timedelta(days=time_range.days)
    filtered = []
    unknown_dates = []
    for item in results:
        parsed = _date_sort_value(item.published_at)
        if not parsed:
            if item.origin == "external":
                unknown_dates.append(item)
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed >= cutoff:
            filtered.append(item)
    return [*filtered, *unknown_dates]


def _merge_results(results: list[SearchResult]) -> list[SearchResult]:
    merged: list[SearchResult] = []
    seen: set[str] = set()
    for item in results:
        key = item.article_id or item.url
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _evidence_payload(store: NewsStore, results: list[SearchResult]) -> list[dict[str, Any]]:
    evidence = []
    for index, item in enumerate(results[:12], start=1):
        row = store.get_article(item.article_id) if item.article_id else None
        content = (row or {}).get("content") or item.summary or ""
        evidence.append(
            {
                "index": index,
                "article_id": item.article_id,
                "source_id": item.source_id,
                "title": item.title,
                "url": item.url,
                "category": item.category,
                "published_at": _date_text(item.published_at),
                "summary": item.summary or (content[:180] if content else ""),
                "content_excerpt": content[:700],
                "origin": item.origin,
                "score": item.score,
            }
        )
    return evidence


async def _maybe_build_topic_view(topic_views: Any, query: str, categories: list[str] | None) -> dict[str, Any] | None:
    if not topic_views:
        return None
    try:
        payload = await topic_views.build(query, categories, None, max_articles=12)
        event_line = payload.get("event_line") or {}
        items = list(event_line.get("items") or [])[:8]
        return {**event_line, "items": items}
    except Exception:
        return None


def _rank_for_chat(results: list[SearchResult], message: str) -> list[SearchResult]:
    seen = set()
    deduped = []
    for item in results:
        key = item.article_id or item.url
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    freshness_intent = any(token in message for token in ("今天", "最新", "新变化", "最近", "现在"))
    if not freshness_intent:
        return deduped
    return sorted(
        deduped,
        key=lambda item: (
            item.origin == "external",
            _date_sort_value(item.published_at) is not None,
            _date_sort_value(item.published_at) or datetime.min,
            item.score,
        ),
        reverse=True,
    )


def _chat_messages(
    message: str,
    query: str,
    categories: list[str] | None,
    results: list[SearchResult],
    history: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    evidence = []
    for idx, item in enumerate(results[:8], start=1):
        date = _date_text(item.published_at) or "unknown"
        evidence.append(
            f"[{idx}] 标题：{item.title}\n来源：{item.source_id}\n日期：{date}\n摘要：{item.summary or ''}\n链接：{item.url}"
        )
    system = (
        "你是个人资讯助手。必须基于给定证据回答，不要编造。"
        "回答要像对话：先给结论，再给证据和可继续追问方向。"
        "如果证据不足，要明确说不足。"
    )
    user = (
        f"近期对话：\n{_conversation_history_text(history)}\n\n"
        f"当前专题：{query}\n"
        f"分类范围：{', '.join(categories or []) or '未限定'}\n"
        f"用户问题：{message}\n\n"
        "证据：\n"
        + "\n\n".join(evidence)
        + "\n\n请用中文回答，控制在 500 字以内。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _research_messages(
    message: str,
    query: str,
    categories: list[str] | None,
    time_range: TimeRange | None,
    evidence: list[dict[str, Any]],
    expanded_queries: list[dict[str, Any]],
    event_line: dict[str, Any] | None,
    trace: list[dict[str, Any]],
    history: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    evidence_text = []
    for item in evidence[:10]:
        evidence_text.append(
            f"[{item['index']}] {item['title']}\n"
            f"来源：{item['source_id']}｜日期：{item.get('published_at') or 'unknown'}｜origin：{item.get('origin')}\n"
            f"摘要：{item.get('summary') or ''}\n"
            f"正文片段：{item.get('content_excerpt') or ''}\n"
            f"链接：{item.get('url') or ''}"
        )
    expansion_text = "\n".join(
        f"- {item.get('query')}（{item.get('direction') or 'unknown'}：{item.get('rationale') or ''}）" for item in expanded_queries[:6]
    )
    timeline_text = "\n".join(
        f"- {item.get('date')}: {item.get('title')}｜{item.get('summary') or ''}" for item in (event_line or {}).get("items", [])[:8]
    )
    trace_text = "\n".join(f"- {item.get('stage')}: {item.get('message')}" for item in trace)
    system = (
        "你是个人资讯研究助手。必须严格基于证据回答，不要补充未在证据出现的事实。"
        "输出 Markdown，先给结论，再按时间/主题归纳，最后列不确定性和可追问方向。"
        "不要重复展示执行过程，执行过程会由系统单独渲染。"
        "如果证据不足，要明确指出不足，不要装作已经完整覆盖。"
    )
    user = (
        f"近期对话：\n{_conversation_history_text(history)}\n\n"
        f"用户问题：{message}\n"
        f"研究主题：{query}\n"
        f"分类范围：{', '.join(categories or []) or '未限定'}\n"
        f"时间范围：近 {time_range.days} 天\n" if time_range else f"用户问题：{message}\n研究主题：{query}\n分类范围：{', '.join(categories or []) or '未限定'}\n时间范围：未限定\n"
    )
    user += (
        f"\n执行摘要：\n{trace_text}\n\n"
        f"扩展查询：\n{expansion_text or '无'}\n\n"
        f"事件线候选：\n{timeline_text or '无'}\n\n"
        "证据：\n"
        + "\n\n".join(evidence_text)
        + "\n\n请用中文输出，不超过 900 字，引用证据时用 [1] 这样的编号。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _dedupe_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for turn in turns:
        key = str(turn.get("id") or f"{turn.get('conversation_id')}:{turn.get('created_at')}:{turn.get('user_message')}")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(turn)
    return deduped


def _focus_topic_text(turn: dict[str, Any]) -> str | None:
    focus = turn.get("focus_object") or {}
    if focus.get("type") != "topic":
        return None
    return focus.get("text")


def _last_turn_with_topic(turns: list[dict[str, Any]]) -> dict[str, Any] | None:
    for turn in reversed(turns):
        if turn.get("topic") or _focus_topic_text(turn):
            return turn
    return None


def _conversation_history_text(
    history: list[dict[str, Any]] | None,
    *,
    turn_limit: int = 4,
    question_limit: int = 240,
    answer_limit: int = 500,
) -> str:
    if not history:
        return "无"
    lines = []
    for turn in history[-turn_limit:]:
        question = str(turn.get("user_message") or "").strip()[:question_limit]
        answer = str(turn.get("assistant_answer") or "").strip()[:answer_limit]
        conversation_id = str(turn.get("conversation_id") or "").strip()
        prefix = f"[{conversation_id}] " if conversation_id else ""
        if question:
            lines.append(f"{prefix}用户：{question}")
        if answer:
            lines.append(f"{prefix}助手：{answer}")
    return "\n".join(lines) or "无"


def _local_agent_context_session_id(conversation_id: str) -> str:
    return f"pna-context-{conversation_id}"


def _clean_contextual_rewrite(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"^```(?:json|text)?", "", text.strip(), flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text.strip()).strip()
    if text.startswith("{"):
        try:
            payload = _decode_json_object(text)
            text = str(payload.get("query") or payload.get("question") or payload.get("rewritten") or "").strip()
        except Exception:
            pass
    text = re.sub(r"^(改写后的问题|完整问题|问题|查询|query|question)\s*[:：]", "", text, flags=re.IGNORECASE).strip()
    text = text.strip(" \t\r\n\"“”'`")
    text = re.sub(r"\s+", " ", text)
    return text[:220]


def _topic_create_request(message: str) -> tuple[bool, str | None]:
    text = str(message or "").strip()
    compact = "".join(text.split())
    commands = ("创建一个新的长期专题任务", "创建新的长期专题任务", "新增长期专题任务")
    if compact in commands:
        return True, None
    for command in commands:
        if not text.startswith(command):
            continue
        suffix = text[len(command):].strip(" \t\r\n:：,，。-—")
        if suffix:
            return True, suffix
    return False, None


def _research_fallback_answer(
    query: str,
    evidence: list[dict[str, Any]],
    expanded_queries: list[dict[str, Any]],
    event_line: dict[str, Any] | None,
    prefix: str | None = None,
) -> str:
    if not evidence:
        lead = f"> {prefix}\n\n" if prefix else ""
        return (
            f"{lead}## {query}\n\n"
            "当前没有召回到可引用证据，因此不生成事实归纳或泛化列表。\n\n"
            "可以补充更具体的对象、平台、时间范围，或打开外部搜索后重试。"
        )
    dates = [item.get("published_at") for item in evidence if item.get("published_at")]
    sources = sorted({item.get("source_id") for item in evidence if item.get("source_id")})
    lead = f"> {prefix}\n\n" if prefix else ""
    bullets = []
    for item in evidence[:5]:
        date = item.get("published_at") or "发布时间未知"
        summary = (item.get("summary") or item.get("content_excerpt") or "")[:180]
        bullets.append(f"- [{item['index']}] {item['title']}（{item['source_id']}，{date}）：{summary or '暂无摘要'}")
    timeline = []
    for item in (event_line or {}).get("items", [])[:5]:
        timeline.append(f"- **{item.get('date') or '发布时间未知'}**：{item.get('title')}{'｜' + item.get('summary', '')[:80] if item.get('summary') else ''}")
    expansions = [item.get("query") for item in expanded_queries[:4] if item.get("query")]
    return (
        f"{lead}## {query}\n\n"
        f"基于当前召回的 {len(evidence)} 条证据，覆盖来源：{', '.join(sources) or '本地新闻引擎'}；"
        f"时间覆盖：{min(dates)} 至 {max(dates)}。\n\n"
        "### 主要线索\n"
        + "\n".join(bullets)
        + ("\n\n### 简版事件线\n" + "\n".join(timeline) if timeline else "")
        + ("\n\n### 已扩展的搜索方向\n" + "\n".join(f"- {item}" for item in expansions) if expansions else "")
        + "\n\n### 不确定性\n- 这是基于当前可抓取、可索引来源的阶段性结论；后续需要由 LLM 判断证据可信度、去重同源转载，并补充更强的一手来源。"
    )


def _date_text(value) -> str:
    parsed = _date_sort_value(value)
    return parsed.date().isoformat() if parsed else ""


def _date_sort_value(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
