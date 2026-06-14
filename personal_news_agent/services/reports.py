from __future__ import annotations

from datetime import datetime
from typing import Any

from personal_news_agent.core.models import ReportResponse
from personal_news_agent.core.text import extract_entities, extract_keywords, stable_id, summarize
from personal_news_agent.services.search import UnifiedSearchService
from personal_news_agent.services.store import NewsStore


class ReportGenerationService:
    def __init__(self, store: NewsStore, search_service: UnifiedSearchService):
        self.store = store
        self.search_service = search_service

    async def generate(self, user_id: str, topic: str, category_scope: list[str], time_range: str = "30d", report_type: str = "timeline_analysis") -> ReportResponse:
        try:
            results = await self.search_service.search(topic, category_scope or None, None, None, max_results=12)
            articles = [self.store.get_article(item.article_id) for item in results if item.article_id]
            rows = [row for row in articles if row]
            combined = "\n".join(f"{row['title']}。{row.get('summary') or row.get('content') or ''}" for row in rows)
            keywords = extract_keywords(combined or topic, limit=10)
            entities = extract_entities(combined or topic, limit=10)
            timeline = self._timeline(rows)
            sections: dict[str, Any] = {
                "一、结论摘要": summarize(combined, 260) or f"{topic}暂无足够本地资料，需要接入外部搜索补充。",
                "二、事件背景": f"围绕“{topic}”检索到 {len(rows)} 篇本地/已入库文章，覆盖板块：{', '.join(category_scope) or '不限'}。",
                "三、关键时间线": timeline,
                "四、相关主体与关系": entities,
                "五、主要争议点/看点": keywords[:5],
                "六、不同来源的主要说法": [
                    {"source_id": row["source_id"], "title": row["title"], "summary": row.get("summary") or ""}
                    for row in rows[:6]
                ],
                "七、可能影响与后续观察指标": ["后续价格/产品动作", "多源报道是否交叉验证", "用户反馈和市场数据变化"],
                "八、来源列表与不确定性说明": "默认结果来自本地已抓取库；外部搜索 provider 未配置时，实时覆盖不足。",
            }
            report = {
                "topic": topic,
                "category_scope": category_scope,
                "report_type": report_type,
                "sections": sections,
                "timeline": timeline,
                "sources": [{"article_id": item.article_id, "source_id": item.source_id, "title": item.title, "url": item.url} for item in results],
            }
            report_id = self.store.save_report(user_id, topic, category_scope, report)
            self.store.log("report_generation", "ok", topic, {"report_id": report_id, "source_count": len(results), "timeline_count": len(timeline)})
            return ReportResponse(report_id=report_id, topic=topic, category_scope=category_scope, sections=sections, timeline=timeline, sources=report["sources"])
        except Exception as exc:
            self.store.log("report_generation", "error", topic, {"error": str(exc), "category_scope": category_scope})
            raise

    def _timeline(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for row in sorted(rows, key=lambda item: item.get("published_at") or item.get("fetched_at") or ""):
            date_text = (row.get("published_at") or row.get("fetched_at") or datetime.utcnow().isoformat())[:10]
            text = f"{row['title']} {row.get('summary') or ''}"
            events.append(
                {
                    "date": date_text,
                    "event": row["title"],
                    "actors": extract_entities(text, limit=4),
                    "related_entities": extract_keywords(text, limit=4),
                    "source_article_ids": [row["id"]],
                    "confidence": 0.72,
                }
            )
        if not events:
            events.append(
                {
                    "date": datetime.utcnow().date().isoformat(),
                    "event": "暂无足够入库资料形成时间线",
                    "actors": [],
                    "related_entities": [],
                    "source_article_ids": [],
                    "confidence": 0.2,
                }
            )
        return events
