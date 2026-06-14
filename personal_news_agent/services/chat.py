from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Awaitable, Callable
from uuid import uuid4

from personal_news_agent.core.categories import CATEGORIES
from personal_news_agent.core.models import ChatResponse, FocusObject, SearchResult, TimeRange
from personal_news_agent.services.llm import LLMClient
from personal_news_agent.services.search import UnifiedSearchService
from personal_news_agent.services.store import NewsStore


ORDINALS = {
    "第一": 1,
    "第二": 2,
    "第三": 3,
    "第四": 4,
    "第五": 5,
    "第1": 1,
    "第2": 2,
    "第3": 3,
    "第4": 4,
    "第5": 5,
}


class NewsChatService:
    def __init__(
        self,
        store: NewsStore,
        search_service: UnifiedSearchService,
        llm_client: LLMClient | None = None,
        native_ingestion: Any | None = None,
        deep_dive: Any | None = None,
        topic_views: Any | None = None,
    ):
        self.store = store
        self.search_service = search_service
        self.llm_client = llm_client or LLMClient()
        self.native_ingestion = native_ingestion
        self.deep_dive = deep_dive
        self.topic_views = topic_views

    async def chat(
        self,
        conversation_id: str | None,
        message: str,
        topic: str | None = None,
        category_scope: list[str] | None = None,
        use_llm: bool = False,
    ) -> ChatResponse:
        conv_id = conversation_id or f"conv_{uuid4().hex[:12]}"
        ordinal = _extract_ordinal(message)
        if ordinal:
            response = await self._article_followup(conv_id, message, ordinal)
        elif use_llm:
            response = await self._research_chat(conv_id, message, topic, category_scope)
        else:
            response = await self._news_search(conv_id, message, topic, category_scope, use_llm)
        self._save_response_turn(response, message)
        return response

    async def chat_events(
        self,
        conversation_id: str | None,
        message: str,
        topic: str | None = None,
        category_scope: list[str] | None = None,
        use_llm: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        conv_id = conversation_id or f"conv_{uuid4().hex[:12]}"
        yield {"type": "start", "conversation_id": conv_id, "message": "开始处理问题。"}
        ordinal = _extract_ordinal(message)
        if ordinal or not use_llm:
            response = await (self._article_followup(conv_id, message, ordinal) if ordinal else self._news_search(conv_id, message, topic, category_scope, use_llm))
            self._save_response_turn(response, message)
            yield {"type": "final", "response": response.model_dump(mode="json")}
            return

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def emit_trace(item: dict[str, Any]) -> None:
            await queue.put({"type": "trace", "item": item})

        async def run_pipeline() -> None:
            try:
                response = await self._research_chat(conv_id, message, topic, category_scope, emit_trace)
                self._save_response_turn(response, message)
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

    def _save_response_turn(self, response: ChatResponse, message: str) -> str:
        return self.store.save_turn(
            response.conversation_id,
            message,
            response.answer,
            [item.model_dump(mode="json") for item in response.recommendations],
            response.focus_object.model_dump(mode="json") if response.focus_object else None,
        )

    async def _news_search(
        self,
        conversation_id: str,
        message: str,
        topic: str | None = None,
        category_scope: list[str] | None = None,
        use_llm: bool = False,
    ) -> ChatResponse:
        query = _query_from_message(message, topic)
        categories = category_scope or _infer_categories(message)
        results = await self.search_service.search(query=query, category_scope=categories, source_scope=None, time_range=None, max_results=20)
        results = _rank_for_chat(_enrich_from_store(self.store, results), message)[:8]
        if use_llm and self.llm_client.configured and results:
            try:
                answer = await self.llm_client.chat(_chat_messages(message, query, categories, results))
                context_relation = "topic_grounded_llm"
            except Exception as exc:
                answer = _grounded_answer(query, message, results, f"模型调用失败，已使用本地证据摘要：{exc}")
                context_relation = "topic_grounded_fallback"
        else:
            answer = _grounded_answer(query, message, results)
            context_relation = "topic_grounded"
        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            context_relation=context_relation,
            focus_object=FocusObject(type="topic", text=query),
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
    ) -> ChatResponse:
        trace: list[dict[str, Any]] = []
        query = _query_from_message(message, topic)
        categories = category_scope or _infer_categories(message)
        time_range = _time_range_from_message(message)
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

        await _add_trace(trace, {"stage": "本地召回", "status": "running", "message": "正在查询 ES 和本地新闻库。"}, on_trace)
        local_results = await self.search_service.search(query, categories, None, time_range, max_results=18, include_remote=False)
        local_results = _rank_for_chat(_filter_by_time(_enrich_from_store(self.store, local_results), time_range), message)
        await _add_trace(trace, {"stage": "本地召回", "status": "completed", "message": f"ES/本地库召回 {len(local_results)} 条候选。", "count": len(local_results)}, on_trace)

        ingest_payload: dict[str, Any] | None = None
        if self.native_ingestion:
            try:
                await _add_trace(trace, {"stage": "源搜索入库", "status": "running", "message": "正在搜索新闻源、抓取正文并写入 URL 管理。"}, on_trace)
                ingest_payload = await self.native_ingestion.ingest(
                    query=query,
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
                        "stage": "源搜索入库",
                        "status": "completed",
                        "message": "完成源搜索、URL 入库、正文抓取和索引写入。",
                        "count": ingest_payload.get("discovered_count", 0),
                        "details": {
                            "discovered": ingest_payload.get("discovered_count", 0),
                            "fetched": ingest_payload.get("fetched_count", 0),
                            "indexed": ingest_payload.get("indexed_count", 0),
                            "mysql_ready": ingest_payload.get("mysql_ready"),
                            "elasticsearch_configured": ingest_payload.get("elasticsearch_configured"),
                        },
                    },
                    on_trace,
                )
            except Exception as exc:
                await _add_trace(trace, {"stage": "源搜索入库", "status": "error", "message": f"源搜索入库失败，继续使用已有证据：{exc}"}, on_trace)
        else:
            await _add_trace(trace, {"stage": "源搜索入库", "status": "skipped", "message": "当前服务未注入源搜索入库模块。"}, on_trace)

        await _add_trace(trace, {"stage": "阅读正文", "status": "running", "message": "正在基于新入库内容重新召回。"}, on_trace)
        refreshed_results = await self.search_service.search(query, categories, None, time_range, max_results=24, include_remote=False)
        refreshed_results = _rank_for_chat(_filter_by_time(_enrich_from_store(self.store, refreshed_results), time_range), message)
        await _add_trace(trace, {"stage": "阅读正文", "status": "completed", "message": f"抓取后重新召回 {len(refreshed_results)} 条候选，进入证据合并。", "count": len(refreshed_results)}, on_trace)

        expanded_queries: list[dict[str, Any]] = []
        expansion_results: list[SearchResult] = []
        if self.deep_dive:
            try:
                await _add_trace(trace, {"stage": "扩展搜索", "status": "running", "message": "正在生成垂直/横向扩展查询。"}, on_trace)
                deep_payload = await self.deep_dive.run(query, categories, None, rounds=1, breadth=4, include_remote=False)
                expanded_queries = list(deep_payload.get("expanded_queries") or [])[:6]
                for expansion in expanded_queries[:2]:
                    expansion_query = expansion.get("query")
                    if not expansion_query:
                        continue
                    results = await self.search_service.search(expansion_query, categories, None, time_range, max_results=5, include_remote=False)
                    expansion_results.extend(results)
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

        merged_results = _merge_results([*refreshed_results, *local_results, *expansion_results])
        merged_results = _rank_for_chat(_filter_by_time(merged_results, time_range), message)[:12]
        evidence = _evidence_payload(self.store, merged_results)
        await _add_trace(trace, {"stage": "证据合并", "status": "completed", "message": f"去重后保留 {len(evidence)} 条可引用证据。", "count": len(evidence)}, on_trace)

        event_line = await self._event_line(query, categories, merged_results)
        if event_line and event_line.get("items"):
            await _add_trace(trace, {"stage": "事件线", "status": "completed", "message": f"生成 {len(event_line.get('items') or [])} 个时间节点。", "count": len(event_line.get("items") or [])}, on_trace)

        if self.llm_client.configured and evidence:
            try:
                await _add_trace(trace, {"stage": "生成回答", "status": "running", "message": "正在组织 markdown 回答。"}, on_trace)
                answer = await self.llm_client.chat(_research_messages(message, query, categories, time_range, evidence, expanded_queries, event_line, trace))
                context_relation = "research_pipeline_llm"
            except Exception as exc:
                answer = _research_fallback_answer(query, evidence, expanded_queries, event_line, f"模型调用失败，已使用本地证据摘要：{exc}")
                context_relation = "research_pipeline_fallback"
        else:
            answer = _research_fallback_answer(query, evidence, expanded_queries, event_line)
            context_relation = "research_pipeline_fallback" if evidence else "research_pipeline_empty"
        await _add_trace(trace, {"stage": "生成回答", "status": "completed", "message": "已生成 markdown 回答。"}, on_trace)

        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            markdown=answer,
            context_relation=context_relation,
            focus_object=FocusObject(type="topic", text=query),
            required_context_items=["research_pipeline", "source_search_ingest", "retrieved_evidence", "event_line"],
            recommendations=merged_results[:8],
            research_trace=trace,
            evidence=evidence,
            expanded_queries=expanded_queries,
            event_line=event_line,
        )

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
            date = _date_text(item.published_at) or "未解析"
            items.append(
                {
                    "id": f"chat_evt_{index}",
                    "date": date,
                    "title": item.title,
                    "summary": item.summary[:180] if item.summary else "",
                    "stage": "证据",
                    "source_article_ids": [item.article_id] if item.article_id else [],
                }
            )
        if items:
            return {"view_type": "event_line", "items": items, "lanes": []}
        return await _maybe_build_topic_view(self.topic_views, query, categories)


async def _add_trace(
    trace: list[dict[str, Any]],
    item: dict[str, Any],
    on_trace: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> None:
    trace.append(item)
    if on_trace:
        await on_trace(item)


def _extract_ordinal(message: str) -> int | None:
    for token, value in ORDINALS.items():
        if token in message:
            return value
    match = re.search(r"第\s*(\d+)\s*条", message)
    return int(match.group(1)) if match else None


def _infer_categories(message: str) -> list[str] | None:
    hints = {
        "时政": "politics",
        "政治": "politics",
        "国际": "politics",
        "乌克兰": "politics",
        "俄罗斯": "politics",
        "俄乌": "politics",
        "战争": "politics",
        "冲突": "politics",
        "经济": "economy",
        "财经": "economy",
        "粮食": "economy",
        "农作物": "economy",
        "能源": "economy",
        "制裁": "economy",
        "科技": "tech",
        "AI": "tech",
        "汽车": "auto",
        "车企": "auto",
        "车型": "auto",
        "新能源车": "auto",
        "智能驾驶": "auto",
        "游戏": "game",
        "电竞": "game",
        "动漫": "anime",
        "番剧": "anime",
        "娱乐": "entertainment",
        "明星": "entertainment",
        "体育": "sports",
        "NBA": "sports",
        "球队": "sports",
        "WSBK": "sports",
        "机车赛事": "sports",
    }
    categories = [category for word, category in hints.items() if word.lower() in message.lower()]
    return sorted(set(categories)) or None


def _query_from_message(message: str, topic: str | None = None) -> str:
    original = message
    for zh, key in CATEGORIES.items():
        message = message.replace(zh, " ")
        message = message.replace(key, " ")
    cleanup = [
        "帮我看看",
        "帮我",
        "看看",
        "了解一下",
        "请你",
        "请",
        "今天",
        "近一个月",
        "过去一个月",
        "一个月",
        "近30天",
        "30天",
        "有什么新闻",
        "有什么新变化",
        "有哪些值得关注的新变化",
        "最新进展",
        "新进展",
        "最新",
        "最近",
        "说说",
        "如何",
        "一下",
        "的",
        "？",
        "?",
    ]
    for token in cleanup:
        message = message.replace(token, " ")
    message = message.replace("圈", " ")
    cleaned = " ".join(message.split())
    if topic and topic.strip() and (_is_generic_chat_query(cleaned) or topic.strip() in original):
        return topic.strip()
    return cleaned if len(cleaned) > 1 else "热点 新闻"


def _is_generic_chat_query(cleaned: str) -> bool:
    compact = cleaned.replace(" ", "")
    if not compact:
        return True
    return compact in {"热点新闻", "新闻", "变化", "进展", "更新", "继续", "展开", "深挖"}


def _time_range_from_message(message: str) -> TimeRange | None:
    if any(token in message for token in ("今天", "今日")):
        return TimeRange(days=1)
    if any(token in message for token in ("近一周", "一周", "7天", "七天")):
        return TimeRange(days=7)
    if any(token in message for token in ("近一个月", "过去一个月", "一个月", "30天", "三十天")):
        return TimeRange(days=31)
    if any(token in message for token in ("近半年", "半年", "6个月", "六个月")):
        return TimeRange(days=180)
    if any(token in message for token in ("最近", "最新", "新进展", "新变化")):
        return TimeRange(days=14)
    return None


def _grounded_answer(query: str, message: str, results: list[SearchResult], prefix: str | None = None) -> str:
    if not results:
        return f"我现在没有在本地新闻库里找到【{query}】的可靠证据。可以先触发源搜索入库，再继续问我。"
    top = results[:5]
    dates = sorted({_date_text(item.published_at) for item in top if _date_text(item.published_at)})
    sources = "、".join(sorted({item.source_id for item in top}))
    bullets = []
    for item in top[:4]:
        summary = (item.summary or "").strip()
        detail = summary[:90] + ("…" if len(summary) > 90 else "")
        date = _date_text(item.published_at) or "未解析发布时间"
        bullets.append(f"- {item.title}（{item.source_id}，{date}）：{detail or '暂无摘要'}")
    lead = prefix + "\n\n" if prefix else ""
    return (
        f"{lead}围绕【{query}】，我现在基于 {len(results)} 条本地证据回答。\n"
        f"时间覆盖：{dates[0] + ' 至 ' + dates[-1] if dates else '部分来源未解析发布时间'}；来源：{sources or '本地库'}。\n\n"
        "当前主要变化：\n"
        + "\n".join(bullets)
        + "\n\n可以继续追问：赛事成绩线、商业/上市传闻线、舆论争议线，或让我把它升级为持续跟踪专题。"
    )


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
            unknown_dates.append(item)
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed >= cutoff:
            filtered.append(item)
    return filtered or results[: min(len(results), 8)] or unknown_dates


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
    return sorted(deduped, key=lambda item: (_date_sort_value(item.published_at) is not None, _date_sort_value(item.published_at) or datetime.min, item.score), reverse=True)


def _chat_messages(message: str, query: str, categories: list[str] | None, results: list[SearchResult]) -> list[dict[str, str]]:
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


def _research_fallback_answer(
    query: str,
    evidence: list[dict[str, Any]],
    expanded_queries: list[dict[str, Any]],
    event_line: dict[str, Any] | None,
    prefix: str | None = None,
) -> str:
    if not evidence:
        return f"## {query}\n\n暂时没有召回到足够可靠的证据。可以先扩大来源、放宽时间范围，或补充更具体的关键词。"
    dates = [item.get("published_at") for item in evidence if item.get("published_at")]
    sources = sorted({item.get("source_id") for item in evidence if item.get("source_id")})
    lead = f"> {prefix}\n\n" if prefix else ""
    bullets = []
    for item in evidence[:5]:
        date = item.get("published_at") or "未解析日期"
        summary = (item.get("summary") or item.get("content_excerpt") or "")[:180]
        bullets.append(f"- [{item['index']}] {item['title']}（{item['source_id']}，{date}）：{summary or '暂无摘要'}")
    timeline = []
    for item in (event_line or {}).get("items", [])[:5]:
        timeline.append(f"- **{item.get('date') or '未解析'}**：{item.get('title')}{'｜' + item.get('summary', '')[:80] if item.get('summary') else ''}")
    expansions = [item.get("query") for item in expanded_queries[:4] if item.get("query")]
    return (
        f"{lead}## {query}\n\n"
        f"基于当前召回的 {len(evidence)} 条证据，覆盖来源：{', '.join(sources) or '本地库'}；"
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
