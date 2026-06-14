from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class SectionConfig:
    key: str
    name: str
    category: str
    url: str
    crawl_strategy: str = "list_page"
    crawl_enabled: bool = True
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchConfig:
    strategy: str = "external_first"
    domain_filters: tuple[str, ...] = ()
    native_search_enabled: bool = False
    candidate_templates: tuple[str, ...] = ()
    api_requests: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class RateLimitConfig:
    min_interval_seconds: int = 5
    max_pages_per_run: int = 30


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    name: str
    root_domain: str
    source_type: str
    priority: int
    crawl_enabled: bool
    search_enabled: bool
    categories: tuple[str, ...]
    tags: tuple[str, ...]
    region: str
    language: str
    credibility: float
    crawl_interval_minutes: int
    sections: tuple[SectionConfig, ...]
    search: SearchConfig
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)


@dataclass(frozen=True)
class RawArticleLink:
    source_id: str
    section_key: str
    url: str
    title: str
    published_at: datetime | None = None


@dataclass(frozen=True)
class RawSearchResult:
    source_id: str
    title: str
    url: str
    snippet: str = ""
    published_at: datetime | None = None


@dataclass(frozen=True)
class RawArticle:
    source_id: str
    url: str
    title: str
    content: str
    summary: str = ""
    author: str | None = None
    published_at: datetime | None = None


@dataclass(frozen=True)
class NormalizedArticle:
    id: str
    source_id: str
    section_key: str | None
    url: str
    title: str
    summary: str
    content: str
    category: str
    published_at: datetime | None
    fetched_at: datetime
    source_priority: int
    keywords: list[str]
    entities: list[str]
    content_hash: str


class TimeRange(BaseModel):
    days: int = Field(default=7, ge=1, le=365)


class SearchResult(BaseModel):
    article_id: str | None = None
    source_id: str
    title: str
    url: str
    summary: str = ""
    category: str
    published_at: datetime | None = None
    score: float = 0.0
    origin: str = "local"


class FeedItem(BaseModel):
    article_id: str
    title: str
    summary: str
    source: str
    published_at: datetime | None
    category: str
    recommend_reason: str
    source_tags: list[str] = []
    matched_profile_terms: list[str] = []
    actions: list[str] = ["ask", "deep_dive", "save", "dislike"]
    score: float


class TopicCluster(BaseModel):
    id: str
    title: str
    category: str
    keywords: list[str]
    entities: list[str]
    article_ids: list[str]
    source_count: int
    article_count: int
    hot_score: float
    first_seen_at: datetime | None
    latest_seen_at: datetime | None


class FocusObject(BaseModel):
    type: str
    source_turn_id: str | None = None
    ordinal: int | None = None
    target_id: str | None = None
    text: str | None = None


class ChatResponse(BaseModel):
    conversation_id: str
    answer: str
    context_relation: str
    focus_object: FocusObject | None = None
    required_context_items: list[str] = []
    recommendations: list[SearchResult] = []
    markdown: str | None = None
    research_trace: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    expanded_queries: list[dict[str, Any]] = []
    event_line: dict[str, Any] | None = None


class ReportResponse(BaseModel):
    report_id: str
    topic: str
    category_scope: list[str]
    sections: dict[str, Any]
    timeline: list[dict[str, Any]]
    sources: list[dict[str, Any]]
