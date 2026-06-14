from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from personal_news_agent.config import settings
from personal_news_agent.core.models import RawArticle, RawArticleLink


class ArticleFetchService:
    def __init__(self, timeout: float = 12.0, verify_ssl: bool | None = None):
        self.timeout = timeout
        self.verify_ssl = settings.http_verify_ssl if verify_ssl is None else verify_ssl

    async def list_links(self, source_id: str, section_key: str, url: str, limit: int = 30, allowed_domains: list[str] | None = None) -> list[RawArticleLink]:
        html = await self._get_text(url)
        soup = BeautifulSoup(html, "html.parser")
        links: list[RawArticleLink] = []
        seen: set[str] = set()
        root_domain = urlparse(url).netloc
        domains = allowed_domains or [root_domain]
        for anchor in soup.find_all("a", href=True):
            title = " ".join(anchor.get_text(" ", strip=True).split())
            href = _unwrap_search_link(urljoin(url, anchor["href"]))
            parsed = urlparse(href)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            if domains and not any(_host_allowed(parsed.netloc, domain) for domain in domains):
                continue
            if not _looks_like_article_url(href):
                continue
            if len(title) < 6 or href in seen:
                continue
            seen.add(href)
            links.append(RawArticleLink(source_id=source_id, section_key=section_key, url=href, title=title))
            if len(links) >= limit:
                break
        return links

    async def fetch_article(self, source_id: str, url: str) -> RawArticle:
        html = await self._get_text(url)
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "template"]):
            tag.decompose()
        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = str(og_title["content"]).strip()
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        content = "\n".join(p for p in paragraphs if len(p) >= 12)
        if not content:
            content = soup.get_text(" ", strip=True)
        summary = ""
        desc = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", property="og:description")
        if desc and desc.get("content"):
            summary = str(desc["content"]).strip()
        return RawArticle(source_id=source_id, url=url, title=title or url, content=content, summary=summary, published_at=_extract_published_at(soup))

    async def _get_text(self, url: str) -> str:
        headers = {
            "User-Agent": "Mozilla/5.0 personal-news-agent/0.1",
            "Accept": "text/html,application/xhtml+xml",
        }
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=headers, verify=self.verify_ssl) as client:
            response = await client.get(url)
            response.raise_for_status()
            response.encoding = response.encoding or "utf-8"
            return response.text


def _host_allowed(host: str, domain: str) -> bool:
    normalized = domain.split(":", 1)[0].removeprefix("www.")
    candidate = host.split(":", 1)[0].removeprefix("www.")
    return candidate == normalized or candidate.endswith("." + normalized)


def _looks_like_article_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    if not path or path in {"/", "/index.html", "/index.shtml"}:
        return False
    if any(marker in path for marker in ["/search", "/tag", "/tags", "/video", "/photo", "/special", "/zt/", "/column"]):
        return False
    article_markers = [".html", ".shtml", ".htm", "/a/", "/n1/", "/c/", "/article/", "/news/", "/20"]
    return any(marker in path for marker in article_markers)


def _unwrap_search_link(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    for key in ("targetpage", "target", "url"):
        values = params.get(key)
        if values and values[0].startswith(("http://", "https://")):
            return unquote(values[0])
    return url


def _extract_published_at(soup: BeautifulSoup) -> datetime | None:
    selectors = [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"property": "og:published_time"}),
        ("meta", {"name": "pubdate"}),
        ("meta", {"name": "publishdate"}),
        ("meta", {"name": "publish_date"}),
        ("meta", {"name": "date"}),
        ("meta", {"name": "datetime"}),
        ("meta", {"name": "dc.date"}),
        ("meta", {"name": "dcterms.created"}),
        ("meta", {"itemprop": "datePublished"}),
    ]
    for tag_name, attrs in selectors:
        tag = soup.find(tag_name, attrs=attrs)
        value = tag.get("content") if tag else None
        parsed = _parse_published_datetime(str(value or ""))
        if parsed:
            return parsed

    time_tag = soup.find("time", attrs={"datetime": True}) or soup.find("time")
    if time_tag:
        parsed = _parse_published_datetime(str(time_tag.get("datetime") or time_tag.get_text(" ", strip=True)))
        if parsed:
            return parsed

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        parsed = _extract_json_ld_date(script.get_text(" ", strip=True))
        if parsed:
            return parsed

    text = soup.get_text(" ", strip=True)
    match = re.search(r"20\d{2}[年/-]\d{1,2}[月/-]\d{1,2}日?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?", text)
    return _parse_published_datetime(match.group(0)) if match else None


def _extract_json_ld_date(raw: str) -> datetime | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    stack = payload if isinstance(payload, list) else [payload]
    while stack:
        item = stack.pop(0)
        if isinstance(item, list):
            stack.extend(item)
            continue
        if not isinstance(item, dict):
            continue
        for key in ("datePublished", "dateCreated", "dateModified", "uploadDate"):
            parsed = _parse_published_datetime(str(item.get(key) or ""))
            if parsed:
                return parsed
        stack.extend(value for value in item.values() if isinstance(value, (dict, list)))
    return None


def _parse_published_datetime(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
        if parsed:
            return _to_utc(parsed)
    except (TypeError, ValueError):
        pass

    normalized = (
        text.replace("年", "-")
        .replace("月", "-")
        .replace("日", "")
        .replace("/", "-")
        .replace("T", " ")
        .replace("Z", "+00:00")
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    candidates = [normalized]
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", normalized):
        candidates.append(f"{normalized} 00:00:00")
    for candidate in candidates:
        try:
            return _to_utc(datetime.fromisoformat(candidate))
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return _to_utc(datetime.strptime(candidate, fmt))
                except ValueError:
                    continue
    return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone(timedelta(hours=8)))
    return value.astimezone(timezone.utc)
