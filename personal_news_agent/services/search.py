from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from personal_news_agent.config import Settings
from personal_news_agent.core.models import RawSearchResult, SearchResult, TimeRange
from personal_news_agent.services.search_index import ArticleSearchIndex
from personal_news_agent.services.source_adapter import ListPageAdapter
from personal_news_agent.core.categories import validate_category
from personal_news_agent.services.source_registry import SourceRegistryService
from personal_news_agent.services.store import NewsStore


class ExternalSearchProvider:
    async def search(self, query: str, domains: list[str], limit: int) -> list[RawSearchResult]:
        return []


class BingSearchProvider(ExternalSearchProvider):
    def __init__(self, api_key: str, endpoint: str):
        self.api_key = api_key
        self.endpoint = endpoint

    async def search(self, query: str, domains: list[str], limit: int) -> list[RawSearchResult]:
        domain_query = " OR ".join(f"site:{domain}" for domain in domains[:8])
        full_query = f"{query} ({domain_query})" if domain_query else query
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(
                self.endpoint,
                headers={"Ocp-Apim-Subscription-Key": self.api_key},
                params={"q": full_query, "count": min(limit, 50), "mkt": "zh-CN"},
            )
            response.raise_for_status()
            payload = response.json()
        results = []
        for item in payload.get("webPages", {}).get("value", []):
            results.append(
                RawSearchResult(
                    source_id=_source_from_domain(item.get("url", "")),
                    title=item.get("name") or item.get("url", ""),
                    url=item.get("url", ""),
                    snippet=item.get("snippet", ""),
                )
            )
        return results[:limit]


def external_provider_from_settings(settings: Settings) -> ExternalSearchProvider:
    if settings.external_search_provider == "bing" and settings.bing_search_key:
        return BingSearchProvider(settings.bing_search_key, settings.bing_search_endpoint)
    return ExternalSearchProvider()


class UnifiedSearchService:
    def __init__(
        self,
        store: NewsStore,
        registry: SourceRegistryService,
        external_provider: ExternalSearchProvider | None = None,
        search_index: ArticleSearchIndex | None = None,
    ):
        self.store = store
        self.registry = registry
        self.external_provider = external_provider or ExternalSearchProvider()
        self.search_index = search_index or ArticleSearchIndex()
        self._index_disabled_until: datetime | None = None

    async def search(
        self,
        query: str,
        category_scope: list[str] | None,
        source_scope: list[str] | None,
        time_range: TimeRange | None,
        max_results: int = 20,
        include_remote: bool = True,
    ) -> list[SearchResult]:
        candidate_limit = max(max_results * 4, 30)
        indexed_rows = []
        if not self._index_disabled_until or datetime.now(timezone.utc) >= self._index_disabled_until:
            try:
                indexed_rows = await self.search_index.search(query=query, category_scope=category_scope, source_scope=source_scope, limit=candidate_limit)
            except Exception as exc:
                self._index_disabled_until = datetime.now(timezone.utc) + timedelta(seconds=60)
                self.store.log("search_index_query", "error", query, {"error": str(exc), "disabled_seconds": 60})
        indexed_rows = [row for row in indexed_rows if _row_relevant(query, row)]
        local_rows: list[tuple[dict, str]] = [(row, "elasticsearch") for row in indexed_rows]
        if len(local_rows) < candidate_limit:
            seen_ids = {row.get("id") for row, _ in local_rows}
            fallback_rows = self.store.search_articles(query=query, category_scope=category_scope, limit=candidate_limit)
            fallback_rows = [row for row in fallback_rows if _row_relevant(query, row)]
            if source_scope:
                fallback_rows = [row for row in fallback_rows if row["source_id"] in source_scope]
            for row in fallback_rows:
                if row["id"] in seen_ids:
                    continue
                local_rows.append((row, "local"))
                seen_ids.add(row["id"])
                if len(local_rows) >= candidate_limit:
                    break
        results = [
            SearchResult(
                article_id=row["id"],
                source_id=row["source_id"],
                title=row["title"],
                url=row["url"],
                summary=row["summary"] or "",
                category=row["category"],
                published_at=row["published_at"],
                score=1.0 - index * 0.01,
                origin=origin,
            )
            for index, (row, origin) in enumerate(local_rows)
        ]

        if include_remote and len(results) < candidate_limit:
            native = await self._search_native_sources(query, category_scope, source_scope, candidate_limit - len(results))
            seen_urls = {item.url for item in results}
            for item in native:
                if item.url in seen_urls or not _raw_result_relevant(query, item):
                    continue
                category = category_scope[0] if category_scope else _category_for_source(self.registry, item.source_id)
                results.append(
                    SearchResult(
                        source_id=item.source_id,
                        title=item.title,
                        url=item.url,
                        summary=item.snippet,
                        category=category,
                        published_at=item.published_at,
                        score=0.75,
                        origin="native",
                    )
                )
                seen_urls.add(item.url)
                if len(results) >= candidate_limit:
                    break

        if len(results) < candidate_limit and category_scope:
            seen_ids = {row.get("id") for row, _ in local_rows}
            seen_urls = {item.url for item in results}
            for category in category_scope:
                fallback_rows = self.store.list_articles(category=category, limit=candidate_limit - len(local_rows))
                fallback_rows = [row for row in fallback_rows if _row_relevant(query, row)]
                if source_scope:
                    fallback_rows = [row for row in fallback_rows if row["source_id"] in source_scope]
                for row in fallback_rows:
                    if row["id"] in seen_ids or row["url"] in seen_urls:
                        continue
                    results.append(
                        SearchResult(
                            article_id=row["id"],
                            source_id=row["source_id"],
                            title=row["title"],
                            url=row["url"],
                            summary=row["summary"] or "",
                            category=row["category"],
                            published_at=row["published_at"],
                            score=0.5 - len(results) * 0.01,
                            origin="local",
                        )
                    )
                    seen_ids.add(row["id"])
                    seen_urls.add(row["url"])
                    if len(results) >= candidate_limit:
                        break
                if len(results) >= candidate_limit:
                    break

        if include_remote and len(results) < candidate_limit:
            domains = self.registry.get_domain_filters(category_scope, source_scope)
            try:
                external = await self.external_provider.search(query, domains, candidate_limit - len(results))
            except Exception as exc:
                self.store.log("news_search", "error", query, {"error": str(exc), "domains": domains})
                external = []
            seen_urls = {item.url for item in results}
            for item in external:
                if item.url in seen_urls or not self._domain_allowed(item.url, domains) or not _raw_result_relevant(query, item):
                    continue
                category = category_scope[0] if category_scope else "tech"
                results.append(
                    SearchResult(
                        source_id=item.source_id,
                        title=item.title,
                        url=item.url,
                        summary=item.snippet,
                        category=category,
                        published_at=item.published_at,
                        score=0.6,
                        origin="external",
                    )
                )
                seen_urls.add(item.url)
                if len(results) >= candidate_limit:
                    break
        results = _rerank_results(query, results)[:max_results]
        self.store.log(
            "news_search",
            "ok",
            query,
            {"category_scope": category_scope or [], "source_scope": source_scope or [], "result_count": len(results)},
        )
        return results

    async def _search_native_sources(self, query: str, category_scope: list[str] | None, source_scope: list[str] | None, limit: int) -> list[RawSearchResult]:
        sources = _select_sources(self.registry, category_scope, source_scope)
        results: list[RawSearchResult] = []
        seen_urls: set[str] = set()
        for source in sources:
            if not source.search_enabled or not source.search.native_search_enabled:
                continue
            try:
                per_source_limit = max(2, limit - len(results)) if source_scope else max(2, min(3, limit - len(results)))
                native_results = await ListPageAdapter(source).search(query, limit=per_source_limit)
            except Exception as exc:
                self.store.log("native_source_search", "error", source.source_id, {"query": query, "error": str(exc)})
                continue
            for item in native_results:
                if item.url in seen_urls:
                    continue
                results.append(item)
                seen_urls.add(item.url)
                if len(results) >= limit:
                    return results
        return results

    def _domain_allowed(self, url: str, domains: list[str]) -> bool:
        if not domains:
            return True
        host = urlparse(url).netloc
        return any(host == domain or host.endswith("." + domain) for domain in domains)


def _source_from_domain(url: str) -> str:
    host = urlparse(url).netloc
    return host.replace("www.", "") or "external"


def _select_sources(registry: SourceRegistryService, category_scope: list[str] | None, source_scope: list[str] | None):
    if source_scope:
        return [registry.get_source(source_id) for source_id in source_scope]
    if category_scope:
        for category in category_scope:
            validate_category(category)
        selected = []
        seen_source_ids: set[str] = set()
        for category in category_scope:
            for source in registry.get_sources_by_category(category):
                if source.source_id in seen_source_ids:
                    continue
                selected.append(source)
                seen_source_ids.add(source.source_id)
        return selected
    return registry.all_sources()


def _category_for_source(registry: SourceRegistryService, source_id: str) -> str:
    try:
        source = registry.get_source(source_id)
    except Exception:
        return "tech"
    return source.categories[0] if source.categories else "tech"


def _row_relevant(query: str, row: dict) -> bool:
    terms = _relevance_terms(query)
    if not terms:
        return True
    text = _compact_text(" ".join(str(row.get(key) or "") for key in ("title", "summary", "content", "keywords")))
    return any(term in text for term in terms)


def _raw_result_relevant(query: str, item: RawSearchResult) -> bool:
    terms = _relevance_terms(query)
    if not terms:
        return True
    text = _compact_text(" ".join([item.title or "", item.snippet or ""]))
    return any(term in text for term in terms)


def _rerank_results(query: str, results: list[SearchResult]) -> list[SearchResult]:
    now = datetime.now(timezone.utc)
    terms = _query_terms(query)

    def sort_key(item: SearchResult) -> tuple[int, float, datetime]:
        title = _compact_text(item.title)
        summary = _compact_text(item.summary)
        text = f"{title} {summary}"
        exact_bonus = 0.28 if terms and terms[0] in title else 0.0
        term_hits = sum(1 for term in terms[1:] if term in text)
        term_bonus = min(0.18, term_hits * 0.04)
        freshness = _freshness_bonus(item.published_at, now)
        return (_origin_rank(item.origin), item.score + exact_bonus + term_bonus + freshness, _as_aware_datetime(item.published_at) or datetime.min.replace(tzinfo=timezone.utc))

    return sorted(results, key=sort_key, reverse=True)


def _freshness_bonus(published_at: datetime | None, now: datetime) -> float:
    published = _as_aware_datetime(published_at)
    if not published:
        return 0.0
    age_hours = max(0.0, (now - published).total_seconds() / 3600)
    if age_hours <= 24:
        return 0.36
    if age_hours <= 48:
        return 0.30
    if age_hours <= 24 * 7:
        return 0.20
    if age_hours <= 24 * 30:
        return 0.08
    return 0.0


def _origin_rank(origin: str) -> int:
    if origin == "elasticsearch":
        return 3
    if origin == "local":
        return 2
    if origin == "native":
        return 1
    return 0


def _as_aware_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _query_terms(query: str) -> list[str]:
    normalized = _compact_text(query)
    terms: list[str] = []
    if len(normalized) >= 2:
        terms.append(normalized)
    for part in query.replace("，", " ").replace(",", " ").split():
        compact = _compact_text(part)
        if len(compact) >= 2 and compact not in terms:
            terms.append(compact)
    return terms


def _relevance_terms(query: str) -> list[str]:
    generic_terms = {
        "新闻",
        "热点",
        "热点新闻",
        "最新",
        "最新新闻",
        "进展",
        "情况",
        "最近",
        "看看",
        "跟踪",
        "关注",
        "资讯",
    }
    return [term for term in _query_terms(query) if term not in generic_terms]


def _compact_text(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())
