from __future__ import annotations

from datetime import datetime, timezone

from personal_news_agent.core.models import FeedItem
from personal_news_agent.services.source_registry import SourceRegistryService
from personal_news_agent.services.store import NewsStore


class PersonalizationService:
    def __init__(self, store: NewsStore, registry: SourceRegistryService):
        self.store = store
        self.registry = registry

    def feed(self, user_id: str, category: str | None = None, limit: int = 20) -> list[FeedItem]:
        profile = self.store.get_profile(user_id)
        preferred_category_order = [str(item).strip() for item in profile.get("preferred_categories") or [] if str(item).strip()]
        preferred_categories = set(preferred_category_order)
        interests = [str(item).strip() for item in profile.get("interests") or [] if str(item).strip()]
        negative = [str(item).strip() for item in profile.get("negative_interests") or [] if str(item).strip()]
        profile_terms = _profile_terms(profile)
        rows = _candidate_rows(self.store, category, preferred_category_order)
        sources = {source.source_id: source for source in self.registry.all_sources()}
        source_names = {source.source_id: source.name for source in sources.values()}
        scored: list[tuple[float, dict, str, list[str], list[str]]] = []
        for row in rows:
            text = f"{row['title']} {row.get('summary') or ''} {row.get('content') or ''}"
            text_lower = text.lower()
            source = sources.get(row["source_id"])
            source_tags = list(source.tags) if source else []
            score = 0.25
            reasons: list[str] = []
            matched: list[str] = []
            if row["category"] in preferred_categories:
                score += 0.3
                reasons.append(f"匹配{row['category']}")
                matched.append(row["category"])
            for keyword in interests:
                if keyword.lower() in text_lower:
                    score += 0.25
                    reasons.append(f"关注词 {keyword}")
                    matched.append(keyword)
            tag_matches = sorted(set(source_tags) & profile_terms)
            if tag_matches:
                score += min(0.25, 0.08 * len(tag_matches))
                reasons.append(f"源标签 {tag_matches[0]}")
                matched.extend(tag_matches)
            if any(keyword.lower() in text_lower for keyword in negative):
                score -= 0.5
                reasons.append("负向词降权")
            if source:
                score += max(0.0, 0.15 - source.priority * 0.02)
                score += min(0.2, max(0.0, source.credibility - 0.5) * 0.2)
            else:
                score += max(0.0, 0.15 - (row.get("source_priority") or 5) * 0.02)
            score += _freshness_boost(row)
            scored.append((score, row, "，".join(reasons[:3]) or "近期热点", source_tags, _dedupe(matched)))
        scored.sort(key=lambda item: item[0], reverse=True)
        ranked = _diversify_scores(scored, preferred_category_order, limit) if category is None else scored[:limit]
        return [
            FeedItem(
                article_id=row["id"],
                title=row["title"],
                summary=row.get("summary") or "",
                source=source_names.get(row["source_id"], row["source_id"]),
                published_at=row.get("published_at"),
                category=row["category"],
                recommend_reason=reason,
                source_tags=source_tags,
                matched_profile_terms=matched_terms,
                score=round(score, 4),
            )
            for score, row, reason, source_tags, matched_terms in ranked
        ]


def _profile_terms(profile: dict) -> set[str]:
    values: list[str] = []
    values.extend(profile.get("preferred_categories") or [])
    values.extend(profile.get("interests") or [])
    values.extend(str(profile.get("self_description") or "").replace("，", " ").replace(",", " ").split())
    aliases = {
        "时政": "politics",
        "体育": "sports",
        "nba": "sports",
        "财经": "economy",
        "经济": "economy",
        "科技": "tech",
        "汽车": "auto",
        "游戏": "game",
        "动漫": "anime",
        "娱乐": "entertainment",
    }
    terms = {str(value).strip().lower() for value in values if str(value).strip()}
    terms |= {aliases[value] for value in list(terms) if value in aliases}
    return terms


def _candidate_rows(store: NewsStore, category: str | None, preferred_categories: list[str]) -> list[dict]:
    rows = store.list_articles(category=category, limit=100)
    if category:
        return rows

    seen = {row["id"] for row in rows}
    for preferred_category in preferred_categories:
        for row in store.list_articles(category=preferred_category, limit=50):
            if row["id"] in seen:
                continue
            rows.append(row)
            seen.add(row["id"])
    return rows


def _freshness_boost(row: dict) -> float:
    raw = row.get("published_at") or row.get("fetched_at")
    if not raw:
        return 0.02
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return 0.02
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
    if age_hours <= 6:
        return 0.12
    if age_hours <= 24:
        return 0.08
    if age_hours <= 72:
        return 0.04
    return 0.0


def _diversify_scores(scored: list[tuple[float, dict, str, list[str], list[str]]], preferred_categories: list[str], limit: int) -> list[tuple[float, dict, str, list[str], list[str]]]:
    if limit <= 0:
        return []
    if not preferred_categories:
        return scored[:limit]

    selected: list[tuple[float, dict, str, list[str], list[str]]] = []
    selected_ids: set[str] = set()
    for category in preferred_categories:
        for item in scored:
            row = item[1]
            if row["id"] in selected_ids:
                continue
            if row["category"] == category:
                selected.append(item)
                selected_ids.add(row["id"])
                break
        if len(selected) >= limit:
            return selected

    for item in scored:
        row = item[1]
        if row["id"] in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(row["id"])
        if len(selected) >= limit:
            break
    return selected


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
