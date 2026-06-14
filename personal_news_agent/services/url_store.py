from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urlparse

from personal_news_agent.config import Settings
from personal_news_agent.core.models import RawArticleLink, SourceConfig


CRAWL_URL_SCHEMA = """
CREATE TABLE IF NOT EXISTS pna_crawl_urls (
  id VARCHAR(64) PRIMARY KEY,
  url_hash VARCHAR(64) NOT NULL UNIQUE,
  url TEXT NOT NULL,
  url_type VARCHAR(20) NOT NULL,
  source_id VARCHAR(80) NOT NULL,
  section_key VARCHAR(80),
  category VARCHAR(40),
  title TEXT,
  tags_json TEXT,
  status VARCHAR(20) DEFAULT 'pending',
  priority INT DEFAULT 5,
  fetch_interval_minutes INT DEFAULT 15,
  first_seen_at DATETIME,
  last_seen_at DATETIME,
  last_fetched_at DATETIME,
  next_fetch_at DATETIME,
  fetch_count INT DEFAULT 0,
  error_count INT DEFAULT 0,
  last_error TEXT,
  article_id VARCHAR(80),
  content_hash VARCHAR(128),
  metadata_json TEXT,
  KEY idx_pna_crawl_due (status, next_fetch_at),
  KEY idx_pna_crawl_source_section (source_id, section_key),
  KEY idx_pna_crawl_category (category)
) DEFAULT CHARSET=utf8mb4;
"""


class CrawlUrlStore:
    ready = False

    def init(self) -> None:
        return None

    def sync_sources(self, sources: list[SourceConfig]) -> None:
        return None

    def list_due(self, category: str | None = None, limit: int = 50, url_type: str | None = "section") -> list[dict[str, Any]]:
        return []

    def upsert_links(self, source: SourceConfig, section_key: str, category: str, links: list[RawArticleLink]) -> None:
        return None

    def mark_fetch_ok(self, url: str, article_id: str | None = None, content_hash: str | None = None, interval_minutes: int | None = None) -> None:
        return None

    def mark_fetch_error(self, url: str, error: str, interval_minutes: int | None = None) -> None:
        return None


class MySQLCrawlUrlStore(CrawlUrlStore):
    def __init__(self, database_url: str, min_interval_minutes: int = 10, max_interval_minutes: int = 20):
        self.database_url = database_url
        self.min_interval_minutes = min_interval_minutes
        self.max_interval_minutes = max_interval_minutes
        self.ready = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "MySQLCrawlUrlStore | None":
        if settings.crawl_url_backend != "mysql" or not settings.crawl_database_url:
            return None
        return cls(
            settings.crawl_database_url,
            min_interval_minutes=settings.crawl_interval_min_minutes,
            max_interval_minutes=settings.crawl_interval_max_minutes,
        )

    @contextmanager
    def connect(self) -> Iterator[Any]:
        import pymysql

        parsed = urlparse(self.database_url)
        if parsed.scheme not in {"mysql+pymysql", "mysql"}:
            raise ValueError("MySQL URL must use mysql+pymysql:// or mysql://")
        query = parse_qs(parsed.query)
        conn = pymysql.connect(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            database=(parsed.path or "/").lstrip("/"),
            charset=query.get("charset", ["utf8mb4"])[0],
            connect_timeout=3,
            read_timeout=8,
            write_timeout=8,
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            with conn.cursor() as cursor:
                for statement in _split_sql(CRAWL_URL_SCHEMA):
                    cursor.execute(statement)
        self.ready = True

    def sync_sources(self, sources: list[SourceConfig]) -> None:
        if not self.ready:
            return
        now = _now()
        with self.connect() as conn:
            with conn.cursor() as cursor:
                for source in sources:
                    for section in source.sections:
                        interval = self._interval(source.crawl_interval_minutes)
                        cursor.execute(
                            """
                            INSERT INTO pna_crawl_urls(
                              id, url_hash, url, url_type, source_id, section_key, category, title, tags_json,
                              status, priority, fetch_interval_minutes, first_seen_at, last_seen_at, next_fetch_at, metadata_json
                            )
                            VALUES (%s, %s, %s, 'section', %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                              source_id=VALUES(source_id), section_key=VALUES(section_key), category=VALUES(category),
                              title=VALUES(title), tags_json=VALUES(tags_json), priority=VALUES(priority),
                              fetch_interval_minutes=VALUES(fetch_interval_minutes), last_seen_at=VALUES(last_seen_at),
                              metadata_json=VALUES(metadata_json)
                            """,
                            (
                                _id(section.url),
                                _url_hash(section.url),
                                section.url,
                                source.source_id,
                                section.key,
                                section.category,
                                section.name,
                                json.dumps(list(dict.fromkeys([*source.tags, *section.tags])), ensure_ascii=False),
                                source.priority,
                                interval,
                                now,
                                now,
                                now,
                                json.dumps({"root_domain": source.root_domain, "crawl_strategy": section.crawl_strategy}, ensure_ascii=False),
                            ),
                        )

    def list_due(self, category: str | None = None, limit: int = 50, url_type: str | None = "section") -> list[dict[str, Any]]:
        if not self.ready:
            return []
        clauses = ["status IN ('pending', 'ok', 'error')", "(next_fetch_at IS NULL OR next_fetch_at <= UTC_TIMESTAMP())"]
        params: list[Any] = []
        if category:
            clauses.append("category = %s")
            params.append(category)
        if url_type:
            clauses.append("url_type = %s")
            params.append(url_type)
        params.append(limit)
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT *
                    FROM pna_crawl_urls
                    WHERE {' AND '.join(clauses)}
                    ORDER BY priority ASC, next_fetch_at ASC, last_fetched_at ASC
                    LIMIT %s
                    """,
                    params,
                )
                rows = cursor.fetchall()
        return [_normalize_row(row) for row in rows]

    def upsert_links(self, source: SourceConfig, section_key: str, category: str, links: list[RawArticleLink]) -> None:
        if not self.ready or not links:
            return
        section = next((item for item in source.sections if item.key == section_key), None)
        tags = list(dict.fromkeys([*source.tags, *(section.tags if section else [])]))
        now = _now()
        interval = self._interval(source.crawl_interval_minutes)
        with self.connect() as conn:
            with conn.cursor() as cursor:
                for link in links:
                    cursor.execute(
                        """
                        INSERT INTO pna_crawl_urls(
                          id, url_hash, url, url_type, source_id, section_key, category, title, tags_json,
                          status, priority, fetch_interval_minutes, first_seen_at, last_seen_at, next_fetch_at, metadata_json
                        )
                        VALUES (%s, %s, %s, 'article', %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                          title=COALESCE(VALUES(title), title), last_seen_at=VALUES(last_seen_at),
                          source_id=VALUES(source_id), section_key=VALUES(section_key), category=VALUES(category),
                          tags_json=VALUES(tags_json), fetch_interval_minutes=VALUES(fetch_interval_minutes)
                        """,
                        (
                            _id(link.url),
                            _url_hash(link.url),
                            link.url,
                            source.source_id,
                            section_key,
                            category,
                            link.title,
                            json.dumps(tags, ensure_ascii=False),
                            source.priority,
                            interval,
                            now,
                            now,
                            now,
                            json.dumps({"published_at": link.published_at.isoformat() if link.published_at else None}, ensure_ascii=False),
                        ),
                    )

    def mark_fetch_ok(self, url: str, article_id: str | None = None, content_hash: str | None = None, interval_minutes: int | None = None) -> None:
        if not self.ready:
            return
        now = _now()
        interval = self._interval(interval_minutes)
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE pna_crawl_urls
                    SET status='ok', last_fetched_at=%s, next_fetch_at=%s, fetch_count=fetch_count+1,
                        error_count=0, last_error=NULL, article_id=COALESCE(%s, article_id), content_hash=COALESCE(%s, content_hash)
                    WHERE url_hash=%s
                    """,
                    (now, _after(interval), article_id, content_hash, _url_hash(url)),
                )

    def mark_fetch_error(self, url: str, error: str, interval_minutes: int | None = None) -> None:
        if not self.ready:
            return
        now = _now()
        interval = self._interval(interval_minutes)
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE pna_crawl_urls
                    SET status='error', last_fetched_at=%s, next_fetch_at=%s, error_count=error_count+1, last_error=%s
                    WHERE url_hash=%s
                    """,
                    (now, _after(interval), error[:1000], _url_hash(url)),
                )

    def _interval(self, interval_minutes: int | None) -> int:
        raw = interval_minutes or 15
        return max(self.min_interval_minutes, min(self.max_interval_minutes, raw))


def _split_sql(sql: str) -> list[str]:
    return [part.strip() for part in sql.split(";") if part.strip()]


def _id(url: str) -> str:
    return "url_" + _url_hash(url)[:32]


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _after(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["tags"] = json.loads(item.get("tags_json") or "[]")
    item["metadata"] = json.loads(item.get("metadata_json") or "{}")
    return item
