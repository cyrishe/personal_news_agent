from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from personal_news_agent.config import Settings


ARTICLE_INDEX_MAPPING = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            "article_id": {"type": "keyword"},
            "source_id": {"type": "keyword"},
            "section_key": {"type": "keyword"},
            "category": {"type": "keyword"},
            "url": {"type": "keyword"},
            "title": {"type": "text", "analyzer": "standard"},
            "summary": {"type": "text", "analyzer": "standard"},
            "content": {"type": "text", "analyzer": "standard"},
            "keywords": {"type": "keyword"},
            "entities": {"type": "keyword"},
            "published_at": {"type": "date"},
            "fetched_at": {"type": "date"},
            "source_priority": {"type": "integer"},
            "content_hash": {"type": "keyword"},
        }
    }
}


class ArticleSearchIndex:
    configured = False

    async def ensure_index(self) -> dict[str, Any]:
        return {"configured": False, "ready": False}

    async def index_article(self, article: dict[str, Any]) -> None:
        return None

    async def search(self, query: str, category_scope: list[str] | None = None, source_scope: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return []

    async def health(self) -> dict[str, Any]:
        return {"configured": False, "ready": False}


class ElasticsearchArticleIndex(ArticleSearchIndex):
    def __init__(self, url: str, index_name: str, timeout_seconds: float = 8.0):
        self.url = url.rstrip("/")
        self.index_name = index_name
        self.timeout_seconds = timeout_seconds
        self.configured = True

    @classmethod
    def from_settings(cls, settings: Settings) -> "ElasticsearchArticleIndex | None":
        if not settings.elasticsearch_url:
            return None
        return cls(settings.elasticsearch_url, settings.elasticsearch_index, settings.elasticsearch_timeout_seconds)

    async def ensure_index(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            existing = await client.get(f"{self.url}/{self.index_name}")
            if existing.status_code == 404:
                created = await client.put(f"{self.url}/{self.index_name}", json=ARTICLE_INDEX_MAPPING)
                created.raise_for_status()
                return {"configured": True, "ready": True, "index": self.index_name, "created": True}
            existing.raise_for_status()
        return {"configured": True, "ready": True, "index": self.index_name, "created": False}

    async def index_article(self, article: dict[str, Any]) -> None:
        doc = {
            "article_id": article["id"],
            "source_id": article["source_id"],
            "section_key": article.get("section_key"),
            "category": article["category"],
            "url": article["url"],
            "title": article["title"],
            "summary": article.get("summary") or "",
            "content": article.get("content") or "",
            "keywords": article.get("keywords") or [],
            "entities": article.get("entities") or [],
            "published_at": _date(article.get("published_at")),
            "fetched_at": _date(article.get("fetched_at")),
            "source_priority": article.get("source_priority") or 5,
            "content_hash": article.get("content_hash"),
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.put(f"{self.url}/{self.index_name}/_doc/{article['id']}", json=doc)
            response.raise_for_status()

    async def search(self, query: str, category_scope: list[str] | None = None, source_scope: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        filters: list[dict[str, Any]] = []
        if category_scope:
            filters.append({"terms": {"category": category_scope}})
        if source_scope:
            filters.append({"terms": {"source_id": source_scope}})
        body = {
            "size": limit,
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["title^4", "summary^2", "content", "keywords^3", "entities^2"],
                            }
                        }
                    ],
                    "filter": filters,
                }
            },
            "sort": [{"_score": "desc"}, {"published_at": {"order": "desc", "missing": "_last"}}],
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(f"{self.url}/{self.index_name}/_search", json=body)
            response.raise_for_status()
            payload = response.json()
        rows = []
        for hit in payload.get("hits", {}).get("hits", []):
            source = hit.get("_source") or {}
            rows.append({**source, "id": source.get("article_id"), "score": hit.get("_score") or 0.0})
        return rows

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(f"{self.url}/_cluster/health")
                response.raise_for_status()
                payload = response.json()
            return {"configured": True, "ready": True, "index": self.index_name, "cluster_status": payload.get("status")}
        except Exception as exc:
            return {"configured": True, "ready": False, "index": self.index_name, "error": str(exc)}


def _date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
