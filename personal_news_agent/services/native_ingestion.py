from __future__ import annotations

from dataclasses import asdict
from dataclasses import replace
from datetime import datetime
from typing import Callable

from personal_news_agent.core.models import NormalizedArticle, RawArticleLink, RawSearchResult, SourceConfig, TimeRange
from personal_news_agent.services.search_index import ArticleSearchIndex
from personal_news_agent.services.source_adapter import ListPageAdapter
from personal_news_agent.services.source_registry import SourceRegistryService
from personal_news_agent.services.store import NewsStore
from personal_news_agent.services.url_store import CrawlUrlStore


class NativeSearchIngestionService:
    def __init__(
        self,
        registry: SourceRegistryService,
        store: NewsStore,
        url_store: CrawlUrlStore | None = None,
        search_index: ArticleSearchIndex | None = None,
        adapter_factory: Callable[[SourceConfig], ListPageAdapter] | None = None,
    ):
        self.registry = registry
        self.store = store
        self.url_store = url_store or CrawlUrlStore()
        self.search_index = search_index or ArticleSearchIndex()
        self.adapter_factory = adapter_factory or (lambda source: ListPageAdapter(source))

    async def ingest(
        self,
        query: str,
        category_scope: list[str] | None = None,
        source_scope: list[str] | None = None,
        max_results: int = 20,
        fetch_articles: int = 10,
        follow_depth: int = 0,
        follow_limit_per_article: int = 2,
        max_sources: int | None = None,
        request_timeout_seconds: float | None = None,
    ) -> dict:
        sources = self._select_sources(category_scope, source_scope)
        if max_sources is not None:
            sources = sources[: max(0, int(max_sources))]
        discovered: list[RawSearchResult] = []
        fetched: list[dict] = []
        errors: list[dict] = []
        seen_urls: set[str] = set()
        fetch_budget = max(0, int(fetch_articles))

        for source in sources:
            if not source.search_enabled or not source.search.native_search_enabled:
                continue
            adapter = self.adapter_factory(source)
            if request_timeout_seconds is not None and hasattr(adapter, "fetcher"):
                adapter.fetcher.timeout = min(float(adapter.fetcher.timeout), float(request_timeout_seconds))
            try:
                per_source_limit = max(2, max_results - len(discovered)) if source_scope else max(2, min(3, max_results - len(discovered)))
                results = await adapter.search(query, limit=per_source_limit)
            except Exception as exc:
                errors.append({"stage": "search", "source_id": source.source_id, "error": str(exc)})
                continue
            section_key, category = _section_context(source, category_scope)
            self.url_store.upsert_links(source, section_key, category, [_search_result_to_link(item, section_key) for item in results])
            for item in results:
                if item.url in seen_urls:
                    continue
                seen_urls.add(item.url)
                discovered.append(item)
                if fetch_budget > 0:
                    fetched.extend(
                        await self._fetch_tree(
                            adapter,
                            source,
                            item.url,
                            section_key,
                            category,
                            follow_depth,
                            follow_limit_per_article,
                            seen_urls,
                            errors,
                            item.published_at,
                        )
                    )
                    fetch_budget = max(0, int(fetch_articles) - len(fetched))
                if len(discovered) >= max_results:
                    break
            if len(discovered) >= max_results:
                break

        self.store.log(
            "native_search_ingest",
            "ok",
            query,
            {
                "category_scope": category_scope or [],
                "source_scope": source_scope or [],
                "discovered": len(discovered),
                "fetched": len(fetched),
                "errors": len(errors),
            },
        )
        return {
            "query": query,
            "category_scope": category_scope or [],
            "source_scope": source_scope or [],
            "discovered_count": len(discovered),
            "fetched_count": len(fetched),
            "indexed_count": sum(1 for item in fetched if item.get("indexed")),
            "mysql_ready": self.url_store.ready,
            "elasticsearch_configured": self.search_index.configured,
            "discovered": [asdict(item) for item in discovered],
            "fetched": fetched,
            "errors": errors,
        }

    async def _fetch_tree(
        self,
        adapter: ListPageAdapter,
        source: SourceConfig,
        url: str,
        section_key: str,
        category: str,
        follow_depth: int,
        follow_limit_per_article: int,
        seen_urls: set[str],
        errors: list[dict],
        published_at_hint: datetime | None = None,
    ) -> list[dict]:
        fetched: list[dict] = []
        fetched_article = await self._fetch_one(adapter, source, url, section_key, category, errors, published_at_hint)
        if not fetched_article:
            return fetched
        fetched.append(fetched_article)

        if follow_depth <= 0 or follow_limit_per_article <= 0:
            return fetched
        try:
            allowed_domains = list(source.search.domain_filters or (source.root_domain,))
            follow_links = await adapter.fetcher.list_links(source.source_id, section_key, url, follow_limit_per_article, allowed_domains)
            self.url_store.upsert_links(source, section_key, category, follow_links)
        except Exception as exc:
            errors.append({"stage": "follow_links", "source_id": source.source_id, "url": url, "error": str(exc)})
            return fetched

        for link in follow_links[:follow_limit_per_article]:
            if link.url in seen_urls:
                continue
            seen_urls.add(link.url)
            child = await self._fetch_one(adapter, source, link.url, section_key, category, errors, link.published_at)
            if child:
                child["followed_from"] = url
                fetched.append(child)
        return fetched

    async def _fetch_one(
        self,
        adapter: ListPageAdapter,
        source: SourceConfig,
        url: str,
        section_key: str,
        category: str,
        errors: list[dict],
        published_at_hint: datetime | None = None,
    ) -> dict | None:
        try:
            raw = await adapter.fetch_article(url)
            if raw.published_at is None and published_at_hint is not None:
                raw = replace(raw, published_at=published_at_hint)
            normalized = adapter.normalize_article(raw, section_key, category)
            self.store.save_article(normalized)
            self.url_store.mark_fetch_ok(url, article_id=normalized.id, content_hash=normalized.content_hash, interval_minutes=source.crawl_interval_minutes)
            indexed = False
            try:
                await self.search_index.index_article(normalized.__dict__)
                indexed = self.search_index.configured
            except Exception as exc:
                errors.append({"stage": "index", "source_id": source.source_id, "url": url, "error": str(exc)})
            return _article_summary(normalized, indexed)
        except Exception as exc:
            self.url_store.mark_fetch_error(url, str(exc), source.crawl_interval_minutes)
            errors.append({"stage": "fetch", "source_id": source.source_id, "url": url, "error": str(exc)})
            return None

    def _select_sources(self, category_scope: list[str] | None, source_scope: list[str] | None) -> list[SourceConfig]:
        if source_scope:
            return [self.registry.get_source(source_id) for source_id in source_scope]
        if category_scope:
            selected: list[SourceConfig] = []
            seen: set[str] = set()
            for category in category_scope:
                for source in self.registry.get_sources_by_category(category):
                    if source.source_id in seen:
                        continue
                    selected.append(source)
                    seen.add(source.source_id)
            return selected
        return self.registry.all_sources()


def _section_context(source: SourceConfig, category_scope: list[str] | None) -> tuple[str, str]:
    category = category_scope[0] if category_scope else source.categories[0]
    for section in source.sections:
        if section.category == category:
            return section.key, section.category
    section = source.sections[0]
    return section.key, section.category


def _search_result_to_link(item: RawSearchResult, section_key: str) -> RawArticleLink:
    return RawArticleLink(source_id=item.source_id, section_key=section_key, url=item.url, title=item.title, published_at=item.published_at)


def _article_summary(article: NormalizedArticle, indexed: bool) -> dict:
    return {
        "article_id": article.id,
        "source_id": article.source_id,
        "section_key": article.section_key,
        "category": article.category,
        "title": article.title,
        "url": article.url,
        "content_chars": len(article.content or ""),
        "indexed": indexed,
    }
