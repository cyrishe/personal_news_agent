from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

from personal_news_agent.core.categories import CATEGORIES, validate_category
from personal_news_agent.core.models import RateLimitConfig, SearchConfig, SectionConfig, SourceConfig


class SourceRegistryError(ValueError):
    pass


class SourceRegistryService:
    def __init__(self, sources_path: Path):
        self.sources_path = sources_path
        self._sources: dict[str, SourceConfig] = {}
        self._sections_by_category: dict[str, list[SectionConfig]] = {key: [] for key in CATEGORIES}

    def load(self) -> None:
        if not self.sources_path.exists():
            raise SourceRegistryError(f"sources.yaml not found: {self.sources_path}")
        payload = yaml.safe_load(self.sources_path.read_text(encoding="utf-8")) or {}
        raw_sources = payload.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise SourceRegistryError("sources.yaml must contain a non-empty 'sources' list")

        sources: dict[str, SourceConfig] = {}
        sections_by_category: dict[str, list[SectionConfig]] = {key: [] for key in CATEGORIES}
        for raw in raw_sources:
            source = self._parse_source(raw)
            if source.source_id in sources:
                raise SourceRegistryError(f"Duplicate source_id: {source.source_id}")
            sources[source.source_id] = source
            for section in source.sections:
                sections_by_category[section.category].append(section)

        self._sources = sources
        self._sections_by_category = sections_by_category

    @property
    def sources(self) -> dict[str, SourceConfig]:
        if not self._sources:
            self.load()
        return self._sources

    def all_sources(self) -> list[SourceConfig]:
        return list(self.sources.values())

    def source_summary(self) -> dict:
        tags: dict[str, int] = {}
        categories: dict[str, int] = {key: 0 for key in CATEGORIES}
        crawlable = 0
        searchable = 0
        for source in self.all_sources():
            crawlable += int(source.crawl_enabled)
            searchable += int(source.search_enabled)
            for category in source.categories:
                categories[category] = categories.get(category, 0) + 1
            for tag in source.tags:
                tags[tag] = tags.get(tag, 0) + 1
        return {
            "source_count": len(self.all_sources()),
            "crawlable_sources": crawlable,
            "searchable_sources": searchable,
            "categories": categories,
            "tags": dict(sorted(tags.items(), key=lambda item: (-item[1], item[0]))),
        }

    def get_source(self, source_id: str) -> SourceConfig:
        try:
            return self.sources[source_id]
        except KeyError as exc:
            raise SourceRegistryError(f"Unknown source_id: {source_id}") from exc

    def get_sources_by_category(self, category: str) -> list[SourceConfig]:
        validate_category(category)
        return [source for source in self.sources.values() if category in source.categories]

    def get_sections_by_category(self, category: str) -> list[SectionConfig]:
        validate_category(category)
        if not self._sections_by_category:
            self.load()
        return list(self._sections_by_category[category])

    def get_domain_filters(self, category_scope: list[str] | None, source_scope: list[str] | None) -> list[str]:
        selected = self._select_sources(category_scope, source_scope)
        domains: list[str] = []
        for source in selected:
            for domain in source.search.domain_filters or (source.root_domain,):
                if domain not in domains:
                    domains.append(domain)
        return domains

    def select_sources_for_profile(self, profile: dict, category: str | None = None) -> list[SourceConfig]:
        preferred_categories = set(profile.get("preferred_categories") or [])
        if category:
            preferred_categories.add(category)
        profile_terms = _profile_terms(profile)
        selected: list[SourceConfig] = []
        for source in self.all_sources():
            if preferred_categories and not (set(source.categories) & preferred_categories):
                continue
            if profile_terms and not (set(source.tags) & profile_terms or set(source.categories) & profile_terms):
                if not preferred_categories:
                    continue
            selected.append(source)
        return selected or self.all_sources()

    def _select_sources(self, category_scope: list[str] | None, source_scope: list[str] | None) -> list[SourceConfig]:
        if source_scope:
            return [self.get_source(source_id) for source_id in source_scope]
        if category_scope:
            for category in category_scope:
                validate_category(category)
            return [source for source in self.sources.values() if set(source.categories) & set(category_scope)]
        return self.all_sources()

    def _parse_source(self, raw: dict) -> SourceConfig:
        required = ["source_id", "name", "root_domain", "source_type", "categories", "sections"]
        missing = [key for key in required if key not in raw]
        if missing:
            raise SourceRegistryError(f"Source is missing required keys {missing}: {raw}")

        categories = tuple(raw["categories"])
        for category in categories:
            validate_category(category)
        if not raw.get("root_domain"):
            raise SourceRegistryError(f"Source {raw['source_id']} has empty root_domain")

        source_id = str(raw["source_id"])
        tags = tuple(_normalize_tags(raw.get("tags"), categories, raw.get("source_type"), raw.get("root_domain")))
        sections = tuple(self._parse_section(source_id, section, categories, tags) for section in raw["sections"])
        search_raw = raw.get("search") or {}
        rate_raw = raw.get("rate_limit") or {}
        crawl_interval = _bounded_interval(int(raw.get("crawl_interval_minutes") or rate_raw.get("crawl_interval_minutes") or _default_interval(categories)))
        return SourceConfig(
            source_id=source_id,
            name=str(raw["name"]),
            root_domain=str(raw["root_domain"]),
            source_type=str(raw["source_type"]),
            priority=int(raw.get("priority", 5)),
            crawl_enabled=bool(raw.get("crawl_enabled", True)),
            search_enabled=bool(raw.get("search_enabled", True)),
            categories=categories,
            tags=tags,
            region=str(raw.get("region", "cn")),
            language=str(raw.get("language", "zh")),
            credibility=float(raw.get("credibility", max(0.1, 1.0 - int(raw.get("priority", 5)) * 0.08))),
            crawl_interval_minutes=crawl_interval,
            sections=sections,
            search=SearchConfig(
                strategy=str(search_raw.get("strategy", "external_first")),
                domain_filters=tuple(search_raw.get("domain_filters") or [raw["root_domain"]]),
                native_search_enabled=bool(search_raw.get("native_search_enabled", False)),
                candidate_templates=tuple(search_raw.get("candidate_templates") or ()),
                api_requests=tuple(search_raw.get("api_requests") or ()),
            ),
            rate_limit=RateLimitConfig(
                min_interval_seconds=int(rate_raw.get("min_interval_seconds", 5)),
                max_pages_per_run=int(rate_raw.get("max_pages_per_run", 30)),
            ),
        )

    def _parse_section(self, source_id: str, raw: dict, source_categories: tuple[str, ...], source_tags: tuple[str, ...]) -> SectionConfig:
        for key in ["key", "name", "category", "url"]:
            if key not in raw:
                raise SourceRegistryError(f"Section in {source_id} is missing required key: {key}")
        category = str(raw["category"])
        validate_category(category)
        if category not in source_categories:
            raise SourceRegistryError(f"Section {source_id}/{raw['key']} category is not listed by source")
        parsed = urlparse(str(raw["url"]))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SourceRegistryError(f"Section {source_id}/{raw['key']} has invalid url: {raw['url']}")
        return SectionConfig(
            key=str(raw["key"]),
            name=str(raw["name"]),
            category=category,
            url=str(raw["url"]),
            crawl_strategy=str(raw.get("crawl_strategy", "list_page")),
            crawl_enabled=bool(raw.get("crawl_enabled", True)),
            tags=tuple(_normalize_tags(raw.get("tags"), (category,), *source_tags)),
        )


def _normalize_tags(*groups) -> list[str]:
    tags: list[str] = []
    for group in groups:
        if not group:
            continue
        values = group if isinstance(group, (list, tuple, set)) else [group]
        for value in values:
            tag = str(value).strip().lower()
            if tag and tag not in tags:
                tags.append(tag)
    return tags


def _default_interval(categories: tuple[str, ...]) -> int:
    return 15


def _bounded_interval(minutes: int) -> int:
    return max(10, min(20, minutes))


def _profile_terms(profile: dict) -> set[str]:
    values: list[str] = []
    values.extend(profile.get("preferred_categories") or [])
    values.extend(profile.get("interests") or [])
    values.extend(str(profile.get("self_description") or "").replace("，", " ").replace(",", " ").split())
    aliases = {
        "时政": "politics",
        "体育": "sports",
        "nba": "sports",
        "汽车": "auto",
        "游戏": "game",
        "动漫": "anime",
        "娱乐": "entertainment",
        "科技": "tech",
        "经济": "economy",
        "财经": "economy",
    }
    terms = {str(value).strip().lower() for value in values if str(value).strip()}
    terms |= {aliases[value] for value in list(terms) if value in aliases}
    return terms
