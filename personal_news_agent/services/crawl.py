from __future__ import annotations

from personal_news_agent.core.models import NormalizedArticle
from personal_news_agent.services.search_index import ArticleSearchIndex
from personal_news_agent.services.source_adapter import ListPageAdapter
from personal_news_agent.services.source_registry import SourceRegistryService
from personal_news_agent.services.store import NewsStore
from personal_news_agent.services.url_store import CrawlUrlStore


class CrawlScheduler:
    def __init__(self, registry: SourceRegistryService, store: NewsStore, url_store: CrawlUrlStore | None = None, search_index: ArticleSearchIndex | None = None):
        self.registry = registry
        self.store = store
        self.url_store = url_store or CrawlUrlStore()
        self.search_index = search_index or ArticleSearchIndex()

    async def crawl_category(self, category: str, per_section_limit: int = 10, fetch_articles: int = 1) -> dict:
        results: list[dict] = []
        for source in self.registry.get_sources_by_category(category):
            if not source.crawl_enabled:
                continue
            adapter = ListPageAdapter(source)
            for section in source.sections:
                if section.category != category or not section.crawl_enabled:
                    continue
                try:
                    links = await adapter.crawl_section(section.key, per_section_limit)
                    self.url_store.upsert_links(source, section.key, section.category, links)
                    saved = 0
                    for link in links:
                        try:
                            raw = await adapter.fetch_article(link.url)
                            normalized = adapter.normalize_article(raw, section.key, section.category)
                            await self._save_article(normalized, source.crawl_interval_minutes)
                            saved += 1
                            if saved >= fetch_articles:
                                break
                        except Exception as exc:
                            self.url_store.mark_fetch_error(link.url, str(exc), source.crawl_interval_minutes)
                            self.store.log("fetch_article", "error", link.url, {"error": str(exc)})
                    self.store.mark_section_crawled(source.source_id, section.key)
                    self.url_store.mark_fetch_ok(section.url, interval_minutes=source.crawl_interval_minutes)
                    results.append({"source_id": source.source_id, "section_key": section.key, "links": len(links), "saved": saved, "tags": list(source.tags)})
                    self.store.log("crawl_section", "ok", f"{source.source_id}:{section.key}", {"links": len(links), "saved": saved})
                except Exception as exc:
                    self.url_store.mark_fetch_error(section.url, str(exc), source.crawl_interval_minutes)
                    results.append({"source_id": source.source_id, "section_key": section.key, "error": str(exc)})
                    self.store.log("crawl_section", "error", f"{source.source_id}:{section.key}", {"error": str(exc)})
        return {"category": category, "results": results}

    def due_plan(self, category: str | None = None, limit: int = 50) -> dict:
        sections = self._due_sections(category=category, limit=limit)
        due = [section for section in sections if section["due"]]
        return {
            "category": category,
            "sections": sections,
            "due_count": len(due),
            "total_returned": len(sections),
            "mysql_ready": self.url_store.ready,
        }

    async def crawl_due(self, category: str | None = None, limit: int = 20, per_section_limit: int = 10, fetch_articles: int = 1) -> dict:
        due_sections = [section for section in self._due_sections(category=category, limit=limit) if section["due"]]
        results: list[dict] = []
        for due in due_sections:
            source = self.registry.get_source(due["source_id"])
            section = next((item for item in source.sections if item.key == due["section_key"]), None)
            if not section:
                continue
            adapter = ListPageAdapter(source)
            try:
                links = await adapter.crawl_section(section.key, per_section_limit)
                self.url_store.upsert_links(source, section.key, section.category, links)
                saved = 0
                for link in links:
                    try:
                        raw = await adapter.fetch_article(link.url)
                        normalized = adapter.normalize_article(raw, section.key, section.category)
                        await self._save_article(normalized, source.crawl_interval_minutes)
                        saved += 1
                        if saved >= fetch_articles:
                            break
                    except Exception as exc:
                        self.url_store.mark_fetch_error(link.url, str(exc), source.crawl_interval_minutes)
                        self.store.log("fetch_article", "error", link.url, {"error": str(exc)})
                self.store.mark_section_crawled(source.source_id, section.key)
                self.url_store.mark_fetch_ok(section.url, interval_minutes=source.crawl_interval_minutes)
                result = {"source_id": source.source_id, "section_key": section.key, "category": section.category, "links": len(links), "saved": saved, "tags": list(source.tags)}
                self.store.log("crawl_due_section", "ok", f"{source.source_id}:{section.key}", result)
                results.append(result)
            except Exception as exc:
                self.url_store.mark_fetch_error(section.url, str(exc), source.crawl_interval_minutes)
                result = {"source_id": source.source_id, "section_key": section.key, "category": section.category, "error": str(exc), "tags": list(source.tags)}
                self.store.log("crawl_due_section", "error", f"{source.source_id}:{section.key}", result)
                results.append(result)
        return {
            "category": category,
            "planned_sections": len(due_sections),
            "results": results,
            "saved_articles": sum(item.get("saved", 0) for item in results),
            "errors": sum(1 for item in results if "error" in item),
            "mysql_ready": self.url_store.ready,
        }

    def _due_sections(self, category: str | None, limit: int) -> list[dict]:
        mysql_due = self.url_store.list_due(category=category, limit=limit, url_type="section")
        if self.url_store.ready:
            return [_mysql_due_to_section(item) for item in mysql_due]
        return self.store.due_sections(category=category, limit=limit)

    async def _save_article(self, article: NormalizedArticle, interval_minutes: int) -> None:
        self.store.save_article(article)
        self.url_store.mark_fetch_ok(article.url, article_id=article.id, content_hash=article.content_hash, interval_minutes=interval_minutes)
        try:
            await self.search_index.index_article(article.__dict__)
        except Exception as exc:
            self.store.log("index_article", "error", article.id, {"error": str(exc), "url": article.url})


def _mysql_due_to_section(row: dict) -> dict:
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "section_key": row["section_key"],
        "name": row.get("title") or row["section_key"],
        "category": row["category"],
        "url": row["url"],
        "crawl_strategy": (row.get("metadata") or {}).get("crawl_strategy"),
        "crawl_enabled": True,
        "last_crawled_at": row.get("last_fetched_at"),
        "source_name": row["source_id"],
        "source_tags": row.get("tags") or [],
        "priority": row.get("priority") or 5,
        "crawl_interval_minutes": row.get("fetch_interval_minutes") or 15,
        "due": True,
    }
