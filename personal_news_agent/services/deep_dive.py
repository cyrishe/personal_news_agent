from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from personal_news_agent.core.text import extract_entities, extract_keywords
from personal_news_agent.services.search import UnifiedSearchService


@dataclass(frozen=True)
class ExpansionQuery:
    query: str
    direction: str
    rationale: str


class DeepDiveService:
    def __init__(self, search_service: UnifiedSearchService):
        self.search_service = search_service

    async def run(
        self,
        query: str,
        category_scope: list[str] | None = None,
        source_scope: list[str] | None = None,
        rounds: int = 2,
        breadth: int = 4,
        include_remote: bool = True,
    ) -> dict[str, Any]:
        seed_results = await self.search_service.search(query, category_scope, source_scope, None, max_results=max(6, breadth * 2), include_remote=include_remote)
        seed_payload = [item.model_dump(mode="json") for item in seed_results]
        relevant_seed = _relevant_results(query, seed_payload)
        expansions = self._expand(query, relevant_seed, breadth, bool(seed_payload), category_scope)
        evidence: list[dict[str, Any]] = []
        for expansion in expansions[: max(1, rounds * breadth)]:
            results = await self.search_service.search(expansion.query, category_scope, source_scope, None, max_results=5, include_remote=include_remote)
            evidence.append(
                {
                    "query": expansion.query,
                    "direction": expansion.direction,
                    "rationale": expansion.rationale,
                    "results": [item.model_dump(mode="json") for item in results],
                }
            )
        return {
            "topic": query,
            "strategy": {
                "vertical": "围绕原主题抽取更具体主体、影响链、时间线和关键词继续搜索。",
                "horizontal": "从召回结果中抽取相邻事件、关联主体、上下游影响和跨领域关键词扩展搜索。",
                "llm_planner": "预留：接入 LLM 后由模型从证据中生成下一轮事件、主体、搜索词和停止条件。",
            },
            "seed_results": seed_payload,
            "relevant_seed_count": len(relevant_seed),
            "expanded_queries": [item.__dict__ for item in expansions],
            "evidence": evidence,
            "next_actions": [
                "把高置信扩展词沉淀为用户专题任务的 watch_keywords。",
                "把重复出现的主体和时间点写入专题时间线。",
                "当新证据不足或查询开始重复时停止扩展。",
            ],
        }

    def _expand(self, query: str, results: list[dict[str, Any]], breadth: int, had_seed_results: bool, category_scope: list[str] | None = None) -> list[ExpansionQuery]:
        if not results:
            return _cold_start_expansions(query, breadth, had_seed_results, category_scope)
        text = " ".join(f"{item.get('title') or ''} {item.get('summary') or ''}" for item in results)
        keywords = [item for item in extract_keywords(text, limit=12) if item not in query]
        entities = [item for item in extract_entities(text, limit=12) if item not in query]
        candidates = _dedupe([*entities, *keywords])
        expansions: list[ExpansionQuery] = []
        for term in candidates[:breadth]:
            expansions.append(ExpansionQuery(query=f"{query} {term}", direction="vertical", rationale=f"围绕 {term} 深挖原主题细节"))
        for term in candidates[breadth : breadth * 2]:
            expansions.append(ExpansionQuery(query=f"{term} 影响 {query}", direction="horizontal", rationale=f"检查 {term} 与原主题的关联事件"))
        if not expansions:
            expansions.append(ExpansionQuery(query=query, direction="vertical", rationale="没有足够新词，复用原查询扩大召回"))
        return expansions


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        cleaned = str(value).strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _relevant_results(query: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    terms = _query_terms(query)
    if not terms:
        return results
    relevant = []
    for item in results:
        text = f"{item.get('title') or ''} {item.get('summary') or ''}".lower()
        if any(term in text for term in terms):
            relevant.append(item)
    return relevant


def _query_terms(query: str) -> list[str]:
    normalized = query.strip().lower().replace("，", " ").replace(",", " ")
    pieces = [piece for piece in normalized.split() if piece]
    terms = [normalized.replace(" ", "")]
    terms.extend(pieces)
    if len(terms[0]) >= 4:
        terms.extend([terms[0][index : index + 2] for index in range(0, len(terms[0]) - 1)])
    return _dedupe([term for term in terms if len(term) >= 2])


def _cold_start_expansions(query: str, breadth: int, had_seed_results: bool, category_scope: list[str] | None = None) -> list[ExpansionQuery]:
    scope = set(category_scope or [])
    compact_query = query.replace(" ", "")
    if "politics" in scope or any(token in compact_query for token in ("俄乌", "乌克兰", "俄罗斯", "战争", "冲突")):
        vertical_terms = ["乌克兰", "俄罗斯", "前线战况", "和平谈判", "无人机袭击", "军事援助", "制裁", "停火"]
        horizontal_terms = ["能源价格", "粮食出口", "黑海航运", "北约", "欧盟援助", "美国援助", "战俘交换", "安全保障"]
    elif "economy" in scope:
        vertical_terms = ["价格", "供应链", "政策", "出口", "进口", "库存", "企业", "市场"]
        horizontal_terms = ["能源", "粮食", "汇率", "通胀", "航运", "产业链", "消费", "投资"]
    elif "auto" in scope:
        vertical_terms = ["车型", "销量", "价格", "智驾", "电池", "交付", "召回", "渠道"]
        horizontal_terms = ["供应链", "出海", "补贴", "竞争对手", "资本市场", "用户口碑", "售后争议", "监管"]
    elif "sports" in scope:
        vertical_terms = ["赛事", "赛程", "球队", "球员", "冠军", "积分榜", "伤病", "专访"]
        horizontal_terms = ["商业合作", "社交平台热度", "转会", "赞助", "训练", "裁判争议", "票房", "直播"]
    else:
        vertical_terms = ["最新进展", "关键主体", "时间线", "政策", "影响", "争议", "回应", "后续"]
        horizontal_terms = ["产业影响", "市场反应", "监管", "国际关联", "上下游", "社交平台热度", "风险", "机会"]
    reason = "内部召回有结果但未命中核心词，先生成保守扩展查询" if had_seed_results else "内部召回无结果，进入冷启动扩展"
    expansions: list[ExpansionQuery] = []
    for term in vertical_terms[:breadth]:
        expansions.append(ExpansionQuery(query=f"{query} {term}", direction="vertical", rationale=f"{reason}：补充 {term} 方向"))
    for term in horizontal_terms[:breadth]:
        expansions.append(ExpansionQuery(query=f"{query} {term}", direction="horizontal", rationale=f"{reason}：横向检查 {term} 关联"))
    return expansions
