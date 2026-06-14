from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_news_agent.config import settings
from personal_news_agent.services.article_fetch import _host_allowed, _looks_like_article_url, _unwrap_search_link
from personal_news_agent.services.source_registry import SourceRegistryService


HOLD_HTML_CANDIDATES = {
    "sina_sports": [
        {
            "url": "https://search.sina.com.cn/?q={query_encoded}&c=sports",
            "note": "搜索页可访问，但目前返回前端壳，通用 HTML 链接抽取拿不到文章。",
        }
    ],
    "sohu_sports": [
        {
            "url": "https://search.sohu.com/?keyword={query_encoded}",
            "note": "搜索页可访问，但目前返回前端壳，需后续确认其数据接口。",
        }
    ],
    "qq_sports": [
        {
            "url": "https://news.qq.com/search?query={query_encoded}",
            "note": "搜索页可访问，但目前返回前端壳，需后续确认其数据接口。",
        }
    ],
}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Audit native search paths/APIs for configured news sources.")
    parser.add_argument("--query", default="张雪机车")
    parser.add_argument("--category", default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--output", type=Path, default=Path("source_search_audit_results.json"))
    args = parser.parse_args()

    registry = SourceRegistryService(settings.sources_path)
    registry.load()
    sources = registry.get_sources_by_category(args.category) if args.category else registry.all_sources()
    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        verify=settings.http_verify_ssl,
        headers={"User-Agent": "Mozilla/5.0 personal-news-agent/0.1", "Accept": "text/html,application/json,*/*"},
    ) as client:
        audits = [await audit_source(client, source, args.query, args.limit) for source in sources]

    payload = {
        "query": args.query,
        "category": args.category,
        "summary": summarize(audits),
        "sources": audits,
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"query": args.query, "category": args.category, **payload["summary"], "output": str(args.output)}, ensure_ascii=False))


async def audit_source(client: httpx.AsyncClient, source, query: str, limit: int) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for template in source.search.candidate_templates:
        checks.append(
            await audit_html(
                client=client,
                source_id=source.source_id,
                url=template.format(query=query, query_encoded=quote(query)),
                allowed_domains=list(source.search.domain_filters or (source.root_domain,)),
                limit=limit,
                configured=True,
            )
        )
    for candidate in HOLD_HTML_CANDIDATES.get(source.source_id, []):
        checks.append(
            await audit_html(
                client=client,
                source_id=source.source_id,
                url=candidate["url"].format(query=query, query_encoded=quote(query)),
                allowed_domains=list(source.search.domain_filters or (source.root_domain,)),
                limit=limit,
                configured=False,
                note=candidate.get("note", ""),
            )
        )
    for request in source.search.api_requests:
        checks.append(await audit_api(client, request, query, limit))

    return {
        "source_id": source.source_id,
        "name": source.name,
        "categories": list(source.categories),
        "native_search_enabled": source.search.native_search_enabled,
        "classification": classify(checks),
        "checks": checks,
    }


async def audit_html(
    client: httpx.AsyncClient,
    source_id: str,
    url: str,
    allowed_domains: list[str],
    limit: int,
    configured: bool,
    note: str = "",
) -> dict[str, Any]:
    check: dict[str, Any] = {"kind": "html", "configured": configured, "url": url, "note": note}
    try:
        response = await client.get(url)
        check.update(
            {
                "status_code": response.status_code,
                "final_url": str(response.url),
                "content_type": response.headers.get("content-type", ""),
                "content_length": len(response.text),
            }
        )
        links, raw_anchor_count = extract_article_links(str(response.url), response.text, source_id, allowed_domains, limit)
        check["raw_anchor_count"] = raw_anchor_count
        check["article_link_count"] = len(links)
        check["sample_links"] = links[:5]
    except Exception as exc:
        check["error"] = str(exc)
        check["article_link_count"] = 0
    return check


async def audit_api(client: httpx.AsyncClient, request: dict[str, Any], query: str, limit: int) -> dict[str, Any]:
    method = str(request.get("method", "GET")).upper()
    url = str(request["url"]).format(query=query, query_encoded=quote(query), limit=limit)
    json_payload = format_value(request.get("json"), query, limit) if request.get("json") is not None else None
    check: dict[str, Any] = {"kind": "api", "configured": True, "method": method, "url": url}
    try:
        response = await client.request(method, url, json=json_payload)
        check.update(
            {
                "status_code": response.status_code,
                "final_url": str(response.url),
                "content_type": response.headers.get("content-type", ""),
                "content_length": len(response.text),
            }
        )
        payload = response.json()
        records = extract_path(payload, str(request.get("results_path", ""))) if request.get("results_path") else []
        if not isinstance(records, list):
            records = []
        title_field = str(request.get("title_field", "title"))
        url_field = str(request.get("url_field", "url"))
        snippet_field = str(request.get("snippet_field", "content"))
        samples = []
        for record in records[:limit]:
            if not isinstance(record, dict) or not record.get(url_field):
                continue
            samples.append(
                {
                    "title": strip_html(str(record.get(title_field) or record.get(url_field))),
                    "url": str(record.get(url_field)),
                    "snippet": strip_html(str(record.get(snippet_field) or ""))[:180],
                }
            )
        check["record_count"] = len(records)
        check["article_link_count"] = len(samples)
        check["sample_links"] = samples[:5]
    except Exception as exc:
        check["error"] = str(exc)
        check["article_link_count"] = 0
    return check


def extract_article_links(base_url: str, html: str, source_id: str, allowed_domains: list[str], limit: int) -> tuple[list[dict[str, str]], int]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    anchors = soup.find_all("a", href=True)
    for anchor in anchors:
        title = " ".join(anchor.get_text(" ", strip=True).split())
        href = _unwrap_search_link(urljoin(base_url, anchor["href"]))
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if allowed_domains and not any(_host_allowed(parsed.netloc, domain) for domain in allowed_domains):
            continue
        if not _looks_like_article_url(href):
            continue
        if len(title) < 4 or href in seen:
            continue
        seen.add(href)
        links.append({"source_id": source_id, "title": title, "url": href})
        if len(links) >= limit:
            break
    return links, len(anchors)


def classify(checks: list[dict[str, Any]]) -> str:
    if not checks:
        return "not_researched"
    if any(item.get("kind") == "html" and item.get("configured") and item.get("article_link_count", 0) > 0 for item in checks):
        return "native_html_ready"
    if any(item.get("kind") == "api" and item.get("article_link_count", 0) > 0 for item in checks):
        return "api_ready"
    if any(item.get("article_link_count", 0) > 0 for item in checks):
        return "candidate_ready"
    if any(item.get("status_code") == 200 for item in checks):
        return "hold_unparsed_or_js_shell"
    return "unavailable"


def summarize(audits: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for audit in audits:
        key = audit["classification"]
        counts[key] = counts.get(key, 0) + 1
    counts["total"] = len(audits)
    return counts


def format_value(value: Any, query: str, limit: int) -> Any:
    if isinstance(value, dict):
        return {key: format_value(item, query, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [format_value(item, query, limit) for item in value]
    if isinstance(value, str):
        formatted = value.format(query=query, query_encoded=quote(query), limit=limit)
        return limit if formatted == str(limit) and value == "{limit}" else formatted
    return value


def extract_path(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if not part:
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def strip_html(value: str) -> str:
    return " ".join(BeautifulSoup(value, "html.parser").get_text(" ", strip=True).split())


if __name__ == "__main__":
    asyncio.run(main())
