from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from personal_news_agent.core.text import extract_entities, extract_keywords, stable_id, summarize
from personal_news_agent.services.search import UnifiedSearchService
from personal_news_agent.services.store import NewsStore


class TopicViewService:
    def __init__(self, store: NewsStore, search_service: UnifiedSearchService):
        self.store = store
        self.search_service = search_service

    async def build(
        self,
        topic: str,
        category_scope: list[str] | None = None,
        source_scope: list[str] | None = None,
        max_articles: int = 16,
    ) -> dict[str, Any]:
        results = await self.search_service.search(topic, category_scope, source_scope, None, max_results=max_articles)
        rows = [self.store.get_article(item.article_id) for item in results if item.article_id]
        articles = [row for row in rows if row]
        if not articles:
            articles = [_synthetic_article(topic)]

        event_line = self._event_line(topic, articles)
        relation_graph = self._relation_graph(topic, articles)
        return {
            "topic": {
                "id": stable_id("topic", topic),
                "title": topic,
                "category_scope": category_scope or [],
                "source_scope": source_scope or [],
                "status": "active",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            "event_line": event_line,
            "relation_graph": relation_graph,
            "source_articles": [
                {
                    "article_id": row["id"],
                    "source_id": row["source_id"],
                    "title": row["title"],
                    "url": row["url"],
                    "published_at": row.get("published_at"),
                    "fetched_at": row.get("fetched_at"),
                    "date_source": "published_at" if row.get("published_at") else "fetched_at",
                }
                for row in articles
                if row.get("id")
            ],
            "build": {
                "mode": "rule_based_seed",
                "llm_extraction": "pending",
                "article_count": len(articles),
            },
        }

    def _event_line(self, topic: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        events = []
        for index, row in enumerate(sorted(rows, key=lambda item: item.get("published_at") or item.get("fetched_at") or "")):
            text = _article_text(row)
            actors = extract_entities(text, limit=4)
            keywords = extract_keywords(text, limit=4)
            date_source = "published_at" if row.get("published_at") else "fetched_at"
            date_text = (row.get("published_at") or row.get("fetched_at") or datetime.now(timezone.utc).isoformat())[:10]
            events.append(
                {
                    "id": stable_id("evt", f"{topic}:{row.get('id') or index}"),
                    "date": date_text,
                    "date_source": date_source,
                    "title": row.get("title") or topic,
                    "summary": summarize(text, 140),
                    "stage": _stage(index, len(rows)),
                    "actors": actors,
                    "keywords": keywords,
                    "confidence": 0.62 if row.get("id") else 0.25,
                    "source_article_ids": [row["id"]] if row.get("id") else [],
                }
            )
        return {
            "view_type": "event_line",
            "items": events,
            "lanes": _lanes(events),
        }

    def _relation_graph(self, topic: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        entity_counter: Counter[str] = Counter()
        entity_articles: dict[str, set[str]] = defaultdict(set)
        pair_counter: Counter[tuple[str, str]] = Counter()
        pair_articles: dict[tuple[str, str], set[str]] = defaultdict(set)

        for row in rows:
            text = _signal_text(row)
            terms = []
            for term in [*extract_entities(text, limit=8), *extract_keywords(text, limit=8)]:
                cleaned = str(term).strip()
                if _is_noise_term(cleaned) or cleaned == topic or cleaned in terms:
                    continue
                terms.append(cleaned)
            for term in terms:
                entity_counter[term] += 1
                if row.get("id"):
                    entity_articles[term].add(row["id"])
            for left_index, left in enumerate(terms[:8]):
                for right in terms[left_index + 1 : 8]:
                    key = tuple(sorted((left, right)))
                    pair_counter[key] += 1
                    if row.get("id"):
                        pair_articles[key].add(row["id"])

        top_terms = [term for term, _ in entity_counter.most_common(10)]
        nodes = [
            {
                "id": "topic",
                "label": topic,
                "type": "topic",
                "weight": max(3, len(rows)),
                "source_article_ids": [row["id"] for row in rows if row.get("id")][:8],
            }
        ]
        for term in top_terms:
            nodes.append(
                {
                    "id": stable_id("node", term),
                    "label": term,
                    "type": _node_type(term),
                    "weight": entity_counter[term],
                    "source_article_ids": sorted(entity_articles[term])[:6],
                }
            )

        node_ids = {node["id"] for node in nodes}
        edges = []
        for term in top_terms[:8]:
            edge_id = stable_id("edge", f"{topic}:{term}")
            edges.append(
                {
                    "id": edge_id,
                    "source": "topic",
                    "target": stable_id("node", term),
                    "label": "相关",
                    "weight": entity_counter[term],
                    "source_article_ids": sorted(entity_articles[term])[:6],
                }
            )
        for (left, right), weight in pair_counter.most_common(10):
            left_id = stable_id("node", left)
            right_id = stable_id("node", right)
            if left_id not in node_ids or right_id not in node_ids:
                continue
            edges.append(
                {
                    "id": stable_id("edge", f"{left}:{right}"),
                    "source": left_id,
                    "target": right_id,
                    "label": "共现",
                    "weight": weight,
                    "source_article_ids": sorted(pair_articles[(left, right)])[:6],
                }
            )

        return {
            "view_type": "relation_graph",
            "nodes": nodes,
            "edges": edges,
            "layout": "radial_seed",
        }


def _article_text(row: dict[str, Any]) -> str:
    return f"{row.get('title') or ''}。{row.get('summary') or ''}。{row.get('content') or ''}"


def _signal_text(row: dict[str, Any]) -> str:
    content = str(row.get("content") or "")
    return f"{row.get('title') or ''}。{row.get('summary') or ''}。{content[:600]}"


def _is_noise_term(term: str) -> bool:
    if len(term) < 2:
        return True
    if term.isdigit():
        return True
    if len(term) > 14 and any(marker in term for marker in ["探访", "影响", "成为", "进行", "推出", "表示", "认为"]):
        return True
    if len(term) > 3 and term.startswith("的"):
        return True
    if len(term) > 6 and term.startswith(("让", "从", "在", "为", "将", "对")):
        return True
    noise_markers = [
        "ICP",
        "备案",
        "许可证",
        "Copyright",
        "新浪",
        "虎扑",
        "央视网",
        "有限公司",
        "登录",
        "注册",
        "客户端",
        "免责声明",
        "广告",
        "举报",
        "近日",
        "记者",
        "特约记者",
        "编辑",
    ]
    return any(marker.lower() in term.lower() for marker in noise_markers)


def _stage(index: int, total: int) -> str:
    if total <= 2:
        return "update"
    if index == 0:
        return "origin"
    if index >= total - 2:
        return "latest"
    return "development"


def _lanes(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    stages = []
    labels = {"origin": "起点", "development": "发展", "latest": "最新", "update": "更新"}
    for event in events:
        stage = event["stage"]
        if stage not in stages:
            stages.append(stage)
    return [{"id": stage, "label": labels.get(stage, stage)} for stage in stages]


def _node_type(term: str) -> str:
    if any(token in term for token in ["公司", "车队", "集团", "官方", "媒体"]):
        return "organization"
    if any(token in term for token in ["中国", "美国", "台湾", "重庆", "西班牙", "捷克", "匈牙利"]):
        return "place"
    if any(token in term for token in ["张雪", "德比斯", "车手", "馆长"]):
        return "person"
    return "concept"


def _synthetic_article(topic: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": "",
        "source_id": "pending",
        "title": topic,
        "summary": "暂无足够入库资料，后续将通过源搜索、抓取和大模型抽取补全。",
        "content": topic,
        "published_at": now,
        "fetched_at": now,
        "url": "",
    }
