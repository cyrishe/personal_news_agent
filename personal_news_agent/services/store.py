from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from personal_news_agent.core.categories import CATEGORIES
from personal_news_agent.core.models import NormalizedArticle, SourceConfig, TopicCluster
from personal_news_agent.core.text import content_hash, extract_entities, extract_keywords, stable_id, summarize


SCHEMA = """
CREATE TABLE IF NOT EXISTS news_sources (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  root_domain TEXT NOT NULL,
  source_type TEXT NOT NULL,
  priority INTEGER DEFAULT 5,
  crawl_enabled INTEGER DEFAULT 1,
  search_enabled INTEGER DEFAULT 1,
  tags_json TEXT,
  region TEXT,
  language TEXT,
  credibility REAL,
  crawl_interval_minutes INTEGER DEFAULT 120,
  config_json TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS news_sections (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  section_key TEXT NOT NULL,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  category TEXT NOT NULL,
  crawl_strategy TEXT NOT NULL,
  crawl_enabled INTEGER DEFAULT 1,
  last_crawled_at TEXT,
  config_json TEXT
);
CREATE TABLE IF NOT EXISTS news_articles (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  section_key TEXT,
  url TEXT NOT NULL UNIQUE,
  canonical_url TEXT,
  url_hash TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT,
  content TEXT,
  author TEXT,
  published_at TEXT,
  fetched_at TEXT,
  category TEXT NOT NULL,
  language TEXT DEFAULT 'zh',
  source_priority INTEGER,
  content_hash TEXT,
  status TEXT DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_news_articles_content_hash ON news_articles(content_hash);
CREATE TABLE IF NOT EXISTS news_topics (
  id TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  summary TEXT,
  keywords_json TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  article_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_news_topics_recent ON news_topics(category, last_seen_at);
CREATE TABLE IF NOT EXISTS news_topic_articles (
  article_id TEXT PRIMARY KEY,
  topic_id TEXT NOT NULL,
  subject TEXT,
  event_summary TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  FOREIGN KEY(topic_id) REFERENCES news_topics(id)
);
CREATE INDEX IF NOT EXISTS idx_news_topic_articles_topic ON news_topic_articles(topic_id);
CREATE VIRTUAL TABLE IF NOT EXISTS news_articles_fts USING fts5(
  article_id UNINDEXED,
  title,
  summary,
  content,
  category UNINDEXED,
  source_id UNINDEXED
);
CREATE TABLE IF NOT EXISTS article_entities (
  id TEXT PRIMARY KEY,
  article_id TEXT NOT NULL,
  entity_text TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  confidence REAL DEFAULT 0.0
);
CREATE TABLE IF NOT EXISTS topic_clusters (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  category TEXT NOT NULL,
  keywords_json TEXT,
  entities_json TEXT,
  article_ids_json TEXT,
  source_count INTEGER,
  article_count INTEGER,
  hot_score REAL,
  first_seen_at TEXT,
  latest_seen_at TEXT,
  status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS event_timelines (
  id TEXT PRIMARY KEY,
  cluster_id TEXT NOT NULL,
  event_date TEXT,
  event_title TEXT NOT NULL,
  event_summary TEXT,
  actors_json TEXT,
  source_article_ids_json TEXT,
  confidence REAL DEFAULT 0.0
);
CREATE TABLE IF NOT EXISTS user_profiles (
  user_id TEXT PRIMARY KEY,
  interests_json TEXT,
  negative_interests_json TEXT,
  preferred_categories_json TEXT,
  preferred_sources_json TEXT,
  output_style TEXT DEFAULT 'concise',
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS pna_users (
  id TEXT PRIMARY KEY,
  username TEXT UNIQUE,
  display_name TEXT NOT NULL,
  email TEXT UNIQUE,
  mobile TEXT UNIQUE,
  real_name TEXT,
  id_card_hash TEXT,
  id_card_masked TEXT,
  realname_verified INTEGER DEFAULT 0,
  realname_provider TEXT,
  realname_request_id TEXT,
  realname_verified_at TEXT,
  password_hash TEXT,
  assistant_prompt TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS pna_user_profiles (
  user_id TEXT PRIMARY KEY,
  self_description TEXT,
  age INTEGER,
  gender TEXT,
  zodiac TEXT,
  preferred_categories_json TEXT,
  watch_keywords_json TEXT,
  negative_keywords_json TEXT,
  model_key TEXT,
  output_style TEXT DEFAULT 'concise',
  onboarding_completed INTEGER DEFAULT 0,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS pna_auth_identities (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_user_id TEXT NOT NULL,
  union_id TEXT,
  raw_json TEXT,
  created_at TEXT,
  updated_at TEXT,
  UNIQUE(provider, provider_user_id)
);
CREATE TABLE IF NOT EXISTS pna_auth_sessions (
  token TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at TEXT,
  expires_at TEXT
);
CREATE TABLE IF NOT EXISTS pna_phone_verification_challenges (
  challenge_id TEXT PRIMARY KEY,
  mobile_hash TEXT NOT NULL,
  code_hash TEXT NOT NULL,
  purpose TEXT NOT NULL DEFAULT 'registration',
  provider TEXT NOT NULL,
  provider_request_id TEXT,
  request_ip_hash TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  resend_after TEXT NOT NULL,
  max_attempts INTEGER NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  send_succeeded_at TEXT,
  send_failed_at TEXT,
  verified_at TEXT,
  consumed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pna_phone_challenge_mobile_created
  ON pna_phone_verification_challenges(mobile_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_pna_phone_challenge_ip_created
  ON pna_phone_verification_challenges(request_ip_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_pna_phone_challenge_expiry
  ON pna_phone_verification_challenges(expires_at);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  email TEXT UNIQUE,
  password_hash TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS auth_identities (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_user_id TEXT NOT NULL,
  union_id TEXT,
  raw_json TEXT,
  created_at TEXT,
  updated_at TEXT,
  UNIQUE(provider, provider_user_id)
);
CREATE TABLE IF NOT EXISTS auth_sessions (
  token TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at TEXT,
  expires_at TEXT
);
CREATE TABLE IF NOT EXISTS user_feedback (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  feedback_type TEXT NOT NULL,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS scheduled_tasks (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  task_type TEXT NOT NULL,
  schedule_cron TEXT NOT NULL,
  topics_json TEXT,
  category_scope_json TEXT,
  source_scope_json TEXT,
  output_style TEXT,
  raw_task_description TEXT,
  parsed_workflow_json TEXT,
  delivery_channel TEXT DEFAULT 'in_app',
  enabled INTEGER DEFAULT 1,
  last_run_at TEXT,
  next_run_at TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT,
  target_type TEXT,
  target_id TEXT,
  delivery_channel TEXT DEFAULT 'in_app',
  payload_json TEXT,
  read_at TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS pna_topics (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  conversation_id TEXT DEFAULT '',
  title TEXT NOT NULL,
  topic_type TEXT NOT NULL DEFAULT 'user',
  category_scope_json TEXT,
  source_scope_json TEXT,
  watch_keywords_json TEXT,
  refresh_schedule TEXT NOT NULL DEFAULT '*/20 * * * *',
  task_id TEXT,
  status TEXT DEFAULT 'active',
  last_refresh_at TEXT,
  created_at TEXT,
  updated_at TEXT,
  UNIQUE(user_id, conversation_id, title, topic_type)
);
CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  user_id TEXT,
  topic TEXT NOT NULL,
  category_scope_json TEXT,
  report_json TEXT NOT NULL,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  title TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'chat',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(user_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, updated_at);
CREATE TABLE IF NOT EXISTS conversation_turns (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  user_id TEXT DEFAULT 'default',
  user_message TEXT NOT NULL,
  assistant_answer TEXT NOT NULL,
  recommendations_json TEXT,
  focus_object_json TEXT,
  payload_json TEXT,
  response_json TEXT,
  topic TEXT,
  category_scope_json TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS operation_logs (
  id TEXT PRIMARY KEY,
  operation TEXT NOT NULL,
  target TEXT,
  status TEXT NOT NULL,
  detail_json TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS service_state (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


class NewsStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            self._ensure_news_source_columns(conn)
            self._ensure_pna_user_columns(conn)
            self._ensure_pna_profile_columns(conn)
            self._ensure_conversation_turn_columns(conn)
            self._ensure_topic_columns(conn)
            self._ensure_news_topic_article_columns(conn)
            self._ensure_scheduled_task_columns(conn)

    def _ensure_news_topic_article_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(news_topic_articles)").fetchall()}
        if "subject" not in existing:
            conn.execute("ALTER TABLE news_topic_articles ADD COLUMN subject TEXT")

    def _ensure_news_source_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(news_sources)").fetchall()}
        columns = {
            "tags_json": "TEXT",
            "region": "TEXT",
            "language": "TEXT",
            "credibility": "REAL",
            "crawl_interval_minutes": "INTEGER DEFAULT 120",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE news_sources ADD COLUMN {name} {definition}")

    def _ensure_pna_user_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(pna_users)").fetchall()}
        columns = {
            "username": "TEXT",
            "mobile": "TEXT",
            "real_name": "TEXT",
            "id_card_hash": "TEXT",
            "id_card_masked": "TEXT",
            "realname_verified": "INTEGER DEFAULT 0",
            "realname_provider": "TEXT",
            "realname_request_id": "TEXT",
            "realname_verified_at": "TEXT",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE pna_users ADD COLUMN {name} {definition}")

    def _ensure_pna_profile_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(pna_user_profiles)").fetchall()}
        columns = {
            "self_description": "TEXT",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE pna_user_profiles ADD COLUMN {name} {definition}")

    def _ensure_conversation_turn_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(conversation_turns)").fetchall()}
        columns = {
            "user_id": "TEXT DEFAULT 'default'",
            "payload_json": "TEXT",
            "response_json": "TEXT",
            "topic": "TEXT",
            "category_scope_json": "TEXT",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE conversation_turns ADD COLUMN {name} {definition}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversation_turns_lookup ON conversation_turns(conversation_id, user_id, created_at)"
        )

    def _ensure_scheduled_task_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()}
        columns = {
            "raw_task_description": "TEXT",
            "parsed_workflow_json": "TEXT",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE scheduled_tasks ADD COLUMN {name} {definition}")

    def _ensure_topic_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(pna_topics)").fetchall()}
        if "conversation_id" in existing:
            return
        conn.execute("ALTER TABLE pna_topics RENAME TO pna_topics_legacy")
        conn.execute(
            """
            CREATE TABLE pna_topics (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              conversation_id TEXT DEFAULT '',
              title TEXT NOT NULL,
              topic_type TEXT NOT NULL DEFAULT 'user',
              category_scope_json TEXT,
              source_scope_json TEXT,
              watch_keywords_json TEXT,
              refresh_schedule TEXT NOT NULL DEFAULT '*/20 * * * *',
              task_id TEXT,
              status TEXT DEFAULT 'active',
              last_refresh_at TEXT,
              created_at TEXT,
              updated_at TEXT,
              UNIQUE(user_id, conversation_id, title, topic_type)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO pna_topics(
              id, user_id, conversation_id, title, topic_type, category_scope_json,
              source_scope_json, watch_keywords_json, refresh_schedule, task_id,
              status, last_refresh_at, created_at, updated_at
            )
            SELECT
              id, user_id, '', title, topic_type, category_scope_json,
              source_scope_json, watch_keywords_json, refresh_schedule, task_id,
              status, last_refresh_at, created_at, updated_at
            FROM pna_topics_legacy
            """
        )
        conn.execute("DROP TABLE pna_topics_legacy")

    def upsert_sources(self, sources: list[SourceConfig]) -> None:
        now = _now()
        with self.connect() as conn:
            for source in sources:
                conn.execute(
                    """
                    INSERT INTO news_sources(id, name, root_domain, source_type, priority, crawl_enabled, search_enabled, tags_json, region, language, credibility, crawl_interval_minutes, config_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      name=excluded.name, root_domain=excluded.root_domain, source_type=excluded.source_type,
                      priority=excluded.priority, crawl_enabled=excluded.crawl_enabled, search_enabled=excluded.search_enabled,
                      tags_json=excluded.tags_json, region=excluded.region, language=excluded.language,
                      credibility=excluded.credibility, crawl_interval_minutes=excluded.crawl_interval_minutes,
                      config_json=excluded.config_json, updated_at=excluded.updated_at
                    """,
                    (
                        source.source_id,
                        source.name,
                        source.root_domain,
                        source.source_type,
                        source.priority,
                        int(source.crawl_enabled),
                        int(source.search_enabled),
                        json.dumps(list(source.tags), ensure_ascii=False),
                        source.region,
                        source.language,
                        source.credibility,
                        source.crawl_interval_minutes,
                        json.dumps(_source_to_dict(source), ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                for section in source.sections:
                    section_id = f"{source.source_id}:{section.key}"
                    conn.execute(
                        """
                        INSERT INTO news_sections(id, source_id, section_key, name, url, category, crawl_strategy, crawl_enabled, config_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                          name=excluded.name, url=excluded.url, category=excluded.category,
                          crawl_strategy=excluded.crawl_strategy, crawl_enabled=excluded.crawl_enabled,
                          config_json=excluded.config_json
                        """,
                        (
                            section_id,
                            source.source_id,
                            section.key,
                            section.name,
                            section.url,
                            section.category,
                            section.crawl_strategy,
                            int(section.crawl_enabled),
                            json.dumps(section.__dict__, ensure_ascii=False),
                        ),
                    )

    def list_source_inventory(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT news_sources.*, COUNT(news_sections.id) AS section_count
                FROM news_sources
                LEFT JOIN news_sections ON news_sections.source_id = news_sources.id
                GROUP BY news_sources.id
                ORDER BY priority ASC, id ASC
                """
            ).fetchall()
        inventory = []
        for row in rows:
            item = _row(row)
            item["tags"] = json.loads(item.get("tags_json") or "[]")
            inventory.append(item)
        return inventory

    def due_sections(self, category: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        params: list[Any] = []
        category_clause = ""
        if category:
            category_clause = "AND news_sections.category = ?"
            params.append(category)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                  news_sections.id,
                  news_sections.source_id,
                  news_sections.section_key,
                  news_sections.name,
                  news_sections.category,
                  news_sections.url,
                  news_sections.crawl_strategy,
                  news_sections.crawl_enabled,
                  news_sections.last_crawled_at,
                  news_sources.name AS source_name,
                  news_sources.tags_json AS source_tags_json,
                  news_sources.priority,
                  news_sources.crawl_interval_minutes,
                  CASE
                    WHEN news_sections.last_crawled_at IS NULL THEN 1
                    WHEN datetime(news_sections.last_crawled_at) <= datetime('now', '-' || news_sources.crawl_interval_minutes || ' minutes') THEN 1
                    ELSE 0
                  END AS due
                FROM news_sections
                JOIN news_sources ON news_sources.id = news_sections.source_id
                WHERE news_sources.crawl_enabled = 1
                  AND news_sections.crawl_enabled = 1
                  {category_clause}
                ORDER BY due DESC, news_sources.priority ASC, news_sections.last_crawled_at ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        sections = []
        for row in rows:
            item = _row(row)
            item["due"] = bool(item["due"])
            item["source_tags"] = json.loads(item.get("source_tags_json") or "[]")
            sections.append(item)
        return sections

    def mark_section_crawled(self, source_id: str, section_key: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE news_sections SET last_crawled_at = ? WHERE source_id = ? AND section_key = ?",
                (_now(), source_id, section_key),
            )

    def find_article_by_url(self, url: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM news_articles WHERE url = ? OR canonical_url = ? LIMIT 1", (url, url)).fetchone()
        return _row(row) if row else None

    def save_article(self, article: NormalizedArticle) -> dict[str, Any]:
        with self.connect() as conn:
            duplicate = None
            if article.content_hash:
                duplicate = conn.execute(
                    "SELECT id, url FROM news_articles WHERE content_hash = ? AND status = 'active' LIMIT 1",
                    (article.content_hash,),
                ).fetchone()
            if duplicate and duplicate["url"] != article.url:
                return {"article_id": duplicate["id"], "created": False, "duplicate": True}
            existed = conn.execute("SELECT id FROM news_articles WHERE url = ? LIMIT 1", (article.url,)).fetchone()
            conn.execute(
                """
                INSERT INTO news_articles(id, source_id, section_key, url, canonical_url, url_hash, title, summary, content, published_at, fetched_at, category, source_priority, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                  title=excluded.title, summary=excluded.summary, content=excluded.content,
                  published_at=excluded.published_at, fetched_at=excluded.fetched_at, category=excluded.category,
                  source_priority=excluded.source_priority, content_hash=excluded.content_hash
                """,
                (
                    article.id,
                    article.source_id,
                    article.section_key,
                    article.url,
                    article.url,
                    stable_id("url", article.url),
                    article.title,
                    article.summary,
                    article.content,
                    _dt(article.published_at),
                    _dt(article.fetched_at),
                    article.category,
                    article.source_priority,
                    article.content_hash,
                ),
            )
            for entity in article.entities:
                entity_id = stable_id("ent", f"{article.id}:{entity}")
                conn.execute(
                    """
                    INSERT OR REPLACE INTO article_entities(id, article_id, entity_text, entity_type, confidence)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (entity_id, article.id, entity, "entity", 0.65),
                )
            conn.execute("DELETE FROM news_articles_fts WHERE article_id = ?", (article.id,))
            conn.execute(
                """
                INSERT INTO news_articles_fts(article_id, title, summary, content, category, source_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (article.id, article.title, article.summary, article.content, article.category, article.source_id),
            )
        return {"article_id": article.id, "created": existed is None, "duplicate": False}

    def list_articles(self, category: str | None = None, limit: int = 50, days: int | None = None) -> list[dict[str, Any]]:
        clauses = [
            "status = 'active'",
            "datetime(COALESCE(published_at, fetched_at)) <= datetime('now', '+10 minutes')",
        ]
        params: list[Any] = []
        if category:
            clauses.append("category = ?")
            params.append(category)
        if days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            clauses.append("(published_at IS NULL OR published_at >= ?)")
            params.append(_dt(cutoff))
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM news_articles
                WHERE {' AND '.join(clauses)}
                ORDER BY COALESCE(published_at, fetched_at) DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row(row) for row in rows]

    def list_unprocessed_topic_articles(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.* FROM news_articles a
                LEFT JOIN news_topic_articles ta ON ta.article_id = a.id
                WHERE a.status = 'active' AND ta.article_id IS NULL AND length(COALESCE(a.content, '')) > 0
                  AND datetime(COALESCE(a.published_at, a.fetched_at)) <= datetime('now', '+10 minutes')
                ORDER BY datetime(COALESCE(a.published_at, a.fetched_at)) DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row(row) for row in rows]

    def list_recent_news_topics(self, category: str, days: int = 5, limit: int = 30) -> list[dict[str, Any]]:
        cutoff = _dt(datetime.now(timezone.utc) - timedelta(days=days))
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM news_topics
                   WHERE category = ? AND status = 'active' AND last_seen_at >= ?
                   ORDER BY last_seen_at DESC LIMIT ?""",
                (category, cutoff, limit),
            ).fetchall()
        items = [_row(row) for row in rows]
        for item in items:
            item["keywords"] = json.loads(item.get("keywords_json") or "[]")
        return items

    def list_trending_news_topics(
        self,
        window_hours: int = 24,
        refresh_window_hours: int = 6,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        cutoff = _dt(now - timedelta(hours=max(1, window_hours)))
        refresh_cutoff = _dt(now - timedelta(hours=max(1, refresh_window_hours)))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  t.id,
                  t.category,
                  t.name AS title,
                  t.summary,
                  t.keywords_json,
                  GROUP_CONCAT(DISTINCT a.id) AS article_ids_csv,
                  COUNT(DISTINCT a.id) AS article_count,
                  COUNT(DISTINCT a.source_id) AS source_count,
                  SUM(CASE WHEN COALESCE(a.published_at, a.fetched_at) >= ? THEN 1 ELSE 0 END) AS recent_update_count,
                  MIN(COALESCE(a.published_at, a.fetched_at)) AS first_seen_at,
                  MAX(COALESCE(a.published_at, a.fetched_at)) AS latest_seen_at
                FROM news_topics t
                JOIN news_topic_articles ta ON ta.topic_id = t.id
                JOIN news_articles a ON a.id = ta.article_id
                WHERE t.status = 'active'
                  AND a.status = 'active'
                  AND COALESCE(a.published_at, a.fetched_at) >= ?
                GROUP BY t.id, t.category, t.name, t.summary, t.keywords_json
                ORDER BY source_count DESC, recent_update_count DESC, latest_seen_at DESC
                LIMIT ?
                """,
                (refresh_cutoff, cutoff, min(max(1, limit), 100)),
            ).fetchall()
        items = [_row(row) for row in rows]
        for item in items:
            item["keywords"] = json.loads(item.pop("keywords_json", None) or "[]")
            item["article_ids"] = [value for value in str(item.pop("article_ids_csv", "") or "").split(",") if value]
        return items

    def merge_article_into_news_topic(self, article_id: str, extraction: dict[str, Any], allowed_topic_ids: set[str]) -> dict[str, Any]:
        now = _now()
        category = str(extraction["category"])
        name = " ".join(str(extraction["topic_name"]).split())[:120]
        requested_id = extraction.get("existing_topic_id")
        topic_id = requested_id if requested_id in allowed_topic_ids else None
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM news_topic_articles WHERE article_id = ?", (article_id,)).fetchone():
                row = conn.execute("SELECT topic_id FROM news_topic_articles WHERE article_id = ?", (article_id,)).fetchone()
                return {"topic_id": row["topic_id"], "merged": True, "already_processed": True}
            if not topic_id:
                match = conn.execute(
                    "SELECT id FROM news_topics WHERE category = ? AND lower(name) = lower(?) AND status = 'active' LIMIT 1",
                    (category, name),
                ).fetchone()
                topic_id = match["id"] if match else stable_id("ntp", f"{category}:{name.lower()}")
            exists = conn.execute("SELECT 1 FROM news_topics WHERE id = ?", (topic_id,)).fetchone()
            if exists:
                conn.execute(
                    """UPDATE news_topics SET summary = ?, keywords_json = ?, last_seen_at = ?, article_count = article_count + 1
                       WHERE id = ?""",
                    (extraction["event_summary"], json.dumps(extraction.get("keywords") or [], ensure_ascii=False), now, topic_id),
                )
            else:
                conn.execute(
                    """INSERT INTO news_topics(id, category, name, summary, keywords_json, first_seen_at, last_seen_at, article_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                    (topic_id, category, name, extraction["event_summary"], json.dumps(extraction.get("keywords") or [], ensure_ascii=False), now, now),
                )
            conn.execute(
                """INSERT INTO news_topic_articles(article_id, topic_id, subject, event_summary, confidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (article_id, topic_id, extraction["subject"], extraction["event_summary"], float(extraction.get("confidence") or 0), now),
            )
        return {"topic_id": topic_id, "merged": bool(exists), "already_processed": False}

    def search_articles(self, query: str, category_scope: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        fts_rows = self._search_articles_fts(query, category_scope, limit)
        if fts_rows:
            return fts_rows
        terms = [term for term in query.split() if term.strip()] or [query]
        clauses = ["status = 'active'"]
        params: list[Any] = []
        like_clauses = []
        for term in terms:
            like_clauses.append("(title LIKE ? OR summary LIKE ? OR content LIKE ?)")
            needle = f"%{term}%"
            params.extend([needle, needle, needle])
        clauses.append("(" + " OR ".join(like_clauses) + ")")
        if category_scope:
            placeholders = ", ".join("?" for _ in category_scope)
            clauses.append(f"category IN ({placeholders})")
            params.extend(category_scope)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM news_articles
                WHERE {' AND '.join(clauses)}
                ORDER BY COALESCE(published_at, fetched_at) DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row(row) for row in rows]

    def _search_articles_fts(self, query: str, category_scope: list[str] | None, limit: int) -> list[dict[str, Any]]:
        normalized_query = " OR ".join(part.replace('"', "") for part in query.split() if part.strip())
        if not normalized_query:
            normalized_query = query.replace('"', "")
        clauses = ["news_articles_fts MATCH ?"]
        params: list[Any] = [normalized_query]
        if category_scope:
            placeholders = ", ".join("?" for _ in category_scope)
            clauses.append(f"news_articles.category IN ({placeholders})")
            params.extend(category_scope)
        params.append(limit)
        try:
            with self.connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT news_articles.*
                    FROM news_articles_fts
                    JOIN news_articles ON news_articles.id = news_articles_fts.article_id
                    WHERE {' AND '.join(clauses)}
                    ORDER BY rank
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            return [_row(row) for row in rows]
        except sqlite3.OperationalError:
            return []

    def get_article(self, article_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM news_articles WHERE id = ?", (article_id,)).fetchone()
        return _row(row) if row else None

    def get_profile(self, user_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pna_user_profiles WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            profile = {
                "user_id": user_id,
                "self_description": "",
                "interests": ["AI", "游戏", "新能源汽车"],
                "negative_interests": [],
                "preferred_categories": ["tech", "game", "auto"],
                "preferred_sources": [],
                "output_style": "简洁分析型",
            }
            self.save_profile(profile)
            return profile
        data = _row(row)
        return {
            "user_id": data["user_id"],
            "self_description": data.get("self_description") or "",
            "age": data.get("age"),
            "gender": data.get("gender"),
            "zodiac": data.get("zodiac"),
            "interests": json.loads(data.get("watch_keywords_json") or "[]"),
            "negative_interests": json.loads(data.get("negative_keywords_json") or "[]"),
            "preferred_categories": json.loads(data["preferred_categories_json"] or "[]"),
            "preferred_sources": [],
            "model_key": data.get("model_key") or "yuanrong-personal-assistant",
            "output_style": data["output_style"],
            "onboarding_completed": bool(data.get("onboarding_completed")),
        }

    def create_user(self, display_name: str, email: str | None, password_hash: str | None) -> dict[str, Any]:
        user_id = stable_id("usr", f"{email or display_name}:{_now()}")
        now = _now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pna_users(id, display_name, email, password_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, display_name, email, password_hash, now, now),
            )
        self.save_profile(
            {
                "user_id": user_id,
                "interests": [],
                "negative_interests": [],
                "preferred_categories": ["tech", "game", "auto"],
                "preferred_sources": [],
                "self_description": "",
                "output_style": "concise",
            }
        )
        return {"id": user_id, "display_name": display_name, "email": email, "created_at": now}

    def create_verified_user(
        self,
        username: str,
        password_hash: str,
        real_name: str,
        mobile: str,
        id_card_hash: str | None,
        id_card_masked: str | None,
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        user_id = stable_id("usr", f"{username}:{mobile}:{_now()}")
        now = _now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pna_users(
                  id, username, display_name, mobile, real_name, id_card_hash, id_card_masked,
                  realname_verified, realname_provider, realname_request_id, realname_verified_at,
                  password_hash, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    username,
                    mobile,
                    real_name,
                    id_card_hash,
                    id_card_masked,
                    int(verification.get("passed", False)),
                    verification.get("provider"),
                    verification.get("request_id"),
                    verification.get("verified_at"),
                    password_hash,
                    now,
                    now,
                ),
            )
        self.save_profile(
            {
                "user_id": user_id,
                "interests": [],
                "negative_interests": [],
                "preferred_categories": ["tech", "game", "auto"],
                "preferred_sources": [],
                "self_description": "",
                "output_style": "concise",
            }
        )
        return {"id": user_id, "username": username, "display_name": username, "mobile": mobile, "created_at": now}

    def create_phone_user(
        self,
        *,
        mobile: str,
        password_hash: str,
        challenge_id: str,
        mobile_hash: str,
        verification_provider: str,
    ) -> dict[str, Any]:
        """Create the phone account and consume its verified challenge atomically."""

        now = _now()
        user_id = stable_id("usr", f"phone:{mobile}:{now}")
        display_name = f"用户 {mobile[:3]}****{mobile[-4:]}"
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                challenge = conn.execute(
                    """
                    SELECT challenge_id
                    FROM pna_phone_verification_challenges
                    WHERE challenge_id = ?
                      AND mobile_hash = ?
                      AND purpose = 'registration'
                      AND send_succeeded_at IS NOT NULL
                      AND verified_at IS NOT NULL
                      AND consumed_at IS NULL
                      AND expires_at > ?
                    LIMIT 1
                    """,
                    (challenge_id, mobile_hash, now),
                ).fetchone()
                if not challenge:
                    raise ValueError("phone_code_consumed_or_expired")
                if conn.execute("SELECT 1 FROM pna_users WHERE mobile = ? LIMIT 1", (mobile,)).fetchone():
                    raise ValueError("mobile_already_registered")
                conn.execute(
                    """
                    INSERT INTO pna_users(
                      id, username, display_name, mobile, password_hash,
                      realname_verified, realname_provider, realname_verified_at,
                      created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        mobile,
                        display_name,
                        mobile,
                        password_hash,
                        verification_provider,
                        now,
                        now,
                        now,
                    ),
                )
                updated = conn.execute(
                    """
                    UPDATE pna_phone_verification_challenges
                    SET consumed_at = ?, updated_at = ?
                    WHERE challenge_id = ? AND consumed_at IS NULL
                    """,
                    (now, now, challenge_id),
                )
                if updated.rowcount != 1:
                    raise ValueError("phone_code_consumed_or_expired")
            except Exception:
                conn.rollback()
                raise
        self.save_profile(
            {
                "user_id": user_id,
                "interests": [],
                "negative_interests": [],
                "preferred_categories": ["tech", "game", "auto"],
                "preferred_sources": [],
                "self_description": "",
                "output_style": "concise",
            }
        )
        return {
            "id": user_id,
            "username": mobile,
            "display_name": display_name,
            "mobile": mobile,
            "created_at": now,
        }

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pna_users WHERE username = ?", (username,)).fetchone()
        return _row(row) if row else None

    def get_user_by_mobile(self, mobile: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pna_users WHERE mobile = ?", (mobile,)).fetchone()
        return _row(row) if row else None

    def delete_phone_user(
        self,
        mobile: str,
        mobile_hash: str | None = None,
        *,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Preview or atomically delete one phone account and its user-owned records.

        Verification challenges are short-lived operational records. They are
        cleaned as a best effort when the caller can identify them, but they
        never block deletion of the account or user-owned data.
        """

        user_tables = (
            "pna_user_profiles",
            "user_profiles",
            "pna_auth_identities",
            "pna_auth_sessions",
            "user_feedback",
            "scheduled_tasks",
            "notifications",
            "pna_topics",
            "reports",
            "conversation_turns",
            "conversations",
        )
        with self.connect() as conn:
            users = conn.execute("SELECT id FROM pna_users WHERE mobile = ?", (mobile,)).fetchall()
            user_ids = [row["id"] for row in users]
            counts: dict[str, int] = {}
            for table in user_tables:
                if user_ids:
                    placeholders = ",".join("?" for _ in user_ids)
                    counts[table] = int(
                        conn.execute(
                            f"SELECT COUNT(*) AS count FROM {table} WHERE user_id IN ({placeholders})",
                            user_ids,
                        ).fetchone()["count"]
                    )
                else:
                    counts[table] = 0
            counts["pna_users"] = len(user_ids)
            if mobile_hash:
                counts["pna_phone_verification_challenges"] = int(
                    conn.execute(
                        "SELECT COUNT(*) AS count FROM pna_phone_verification_challenges WHERE mobile_hash = ?",
                        (mobile_hash,),
                    ).fetchone()["count"]
                )
            result = {
                "mobile_masked": f"{mobile[:3]}****{mobile[-4:]}",
                "user_ids": user_ids,
                "counts": counts,
                "deleted": False,
            }
            if not confirm:
                return result

            conn.execute("BEGIN IMMEDIATE")
            try:
                if user_ids:
                    placeholders = ",".join("?" for _ in user_ids)
                    for table in user_tables:
                        conn.execute(f"DELETE FROM {table} WHERE user_id IN ({placeholders})", user_ids)
                    conn.execute(f"DELETE FROM pna_users WHERE id IN ({placeholders})", user_ids)
                if mobile_hash:
                    conn.execute(
                        "DELETE FROM pna_phone_verification_challenges WHERE mobile_hash = ?",
                        (mobile_hash,),
                    )
            except Exception:
                conn.rollback()
                raise
            result["deleted"] = True
            return result

    def change_phone_user(
        self,
        old_mobile: str,
        new_mobile: str,
        old_mobile_hash: str,
        new_mobile_hash: str,
        *,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Preview or change a phone login while preserving the account user id and password."""

        with self.connect() as conn:
            user = conn.execute("SELECT * FROM pna_users WHERE mobile = ? LIMIT 1", (old_mobile,)).fetchone()
            if not user:
                raise ValueError("source_mobile_not_registered")
            conflict = conn.execute("SELECT id FROM pna_users WHERE mobile = ? LIMIT 1", (new_mobile,)).fetchone()
            if conflict and conflict["id"] != user["id"]:
                raise ValueError("target_mobile_already_registered")

            result = {
                "user_id": user["id"],
                "old_mobile_masked": f"{old_mobile[:3]}****{old_mobile[-4:]}",
                "new_mobile_masked": f"{new_mobile[:3]}****{new_mobile[-4:]}",
                "password_preserved": True,
                "changed": False,
            }
            if not confirm:
                return result

            now = _now()
            username = new_mobile if user["username"] == old_mobile else user["username"]
            old_default_name = f"用户 {old_mobile[:3]}****{old_mobile[-4:]}"
            display_name = (
                f"用户 {new_mobile[:3]}****{new_mobile[-4:]}"
                if user["display_name"] == old_default_name
                else user["display_name"]
            )
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE pna_users SET username = ?, display_name = ?, mobile = ?, updated_at = ? WHERE id = ?",
                    (username, display_name, new_mobile, now, user["id"]),
                )
                conn.execute(
                    "DELETE FROM pna_phone_verification_challenges WHERE mobile_hash IN (?, ?)",
                    (old_mobile_hash, new_mobile_hash),
                )
            except Exception:
                conn.rollback()
                raise
            result["changed"] = True
            return result

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pna_users WHERE email = ?", (email,)).fetchone()
        return _row(row) if row else None

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pna_users WHERE id = ?", (user_id,)).fetchone()
        return _row(row) if row else None

    def upsert_auth_identity(self, provider: str, provider_user_id: str, display_name: str, union_id: str | None, raw: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT pna_users.* FROM pna_auth_identities JOIN pna_users ON pna_users.id = pna_auth_identities.user_id WHERE provider = ? AND provider_user_id = ?",
                (provider, provider_user_id),
            ).fetchone()
            if existing:
                user = _row(existing)
                conn.execute(
                    "UPDATE pna_auth_identities SET union_id = ?, raw_json = ?, updated_at = ? WHERE provider = ? AND provider_user_id = ?",
                    (union_id, json.dumps(raw, ensure_ascii=False), now, provider, provider_user_id),
                )
                return user
            user_id = stable_id("usr", f"{provider}:{provider_user_id}:{now}")
            conn.execute(
                "INSERT INTO pna_users(id, display_name, email, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, display_name or "微信用户", None, None, now, now),
            )
            conn.execute(
                """
                INSERT INTO pna_auth_identities(id, user_id, provider, provider_user_id, union_id, raw_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_id("aid", f"{provider}:{provider_user_id}"),
                    user_id,
                    provider,
                    provider_user_id,
                    union_id,
                    json.dumps(raw, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        self.save_profile(
            {
                "user_id": user_id,
                "interests": [],
                "negative_interests": [],
                "preferred_categories": ["tech", "game", "auto"],
                "preferred_sources": [],
                "self_description": "",
                "output_style": "concise",
            }
        )
        return {"id": user_id, "display_name": display_name or "微信用户", "email": None, "created_at": now}

    def create_session(self, user_id: str, token: str, expires_at: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO pna_auth_sessions(token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, user_id, _now(), expires_at),
            )

    def save_profile(self, profile: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pna_user_profiles(user_id, self_description, age, gender, zodiac, preferred_categories_json, watch_keywords_json, negative_keywords_json, model_key, output_style, onboarding_completed, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  self_description=excluded.self_description,
                  age=COALESCE(excluded.age, pna_user_profiles.age),
                  gender=COALESCE(excluded.gender, pna_user_profiles.gender),
                  zodiac=COALESCE(excluded.zodiac, pna_user_profiles.zodiac),
                  preferred_categories_json=excluded.preferred_categories_json,
                  watch_keywords_json=excluded.watch_keywords_json,
                  negative_keywords_json=excluded.negative_keywords_json,
                  model_key=COALESCE(excluded.model_key, pna_user_profiles.model_key),
                  output_style=excluded.output_style,
                  onboarding_completed=excluded.onboarding_completed,
                  updated_at=excluded.updated_at
                """,
                (
                    profile["user_id"],
                    profile.get("self_description", ""),
                    profile.get("age"),
                    profile.get("gender"),
                    profile.get("zodiac"),
                    json.dumps(profile.get("preferred_categories", []), ensure_ascii=False),
                    json.dumps(profile.get("interests", []), ensure_ascii=False),
                    json.dumps(profile.get("negative_interests", []), ensure_ascii=False),
                    profile.get("model_key", "yuanrong-personal-assistant"),
                    profile.get("output_style", "concise"),
                    int(profile.get("onboarding_completed", False)),
                    _now(),
                ),
            )

    def complete_onboarding(self, user_id: str, profile: dict[str, Any], assistant_prompt: str) -> dict[str, Any]:
        now = _now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE pna_users SET display_name = COALESCE(?, display_name), assistant_prompt = ?, updated_at = ? WHERE id = ?",
                (profile.get("display_name"), assistant_prompt, now, user_id),
            )
        profile_payload = {
            "user_id": user_id,
            "self_description": profile.get("self_description", ""),
            "age": profile.get("age"),
            "gender": profile.get("gender"),
            "zodiac": profile.get("zodiac"),
            "interests": profile.get("watch_keywords", []),
            "negative_interests": profile.get("negative_keywords", []),
            "preferred_categories": profile.get("preferred_categories", []),
            "model_key": profile.get("model_key", "yuanrong-personal-assistant"),
            "output_style": profile.get("output_style", "concise"),
            "onboarding_completed": True,
        }
        self.save_profile(profile_payload)
        return self.get_profile(user_id) | {"assistant_prompt": assistant_prompt}

    def save_feedback(self, user_id: str, target_type: str, target_id: str, feedback_type: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO user_feedback(id, user_id, target_type, target_id, feedback_type, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (stable_id("fb", f"{user_id}:{target_type}:{target_id}:{feedback_type}:{_now()}"), user_id, target_type, target_id, feedback_type, _now()),
            )

    def save_cluster(self, cluster: TopicCluster) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO topic_clusters(id, title, category, keywords_json, entities_json, article_ids_json, source_count, article_count, hot_score, first_seen_at, latest_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cluster.id,
                    cluster.title,
                    cluster.category,
                    json.dumps(cluster.keywords, ensure_ascii=False),
                    json.dumps(cluster.entities, ensure_ascii=False),
                    json.dumps(cluster.article_ids, ensure_ascii=False),
                    cluster.source_count,
                    cluster.article_count,
                    cluster.hot_score,
                    _dt(cluster.first_seen_at),
                    _dt(cluster.latest_seen_at),
                ),
            )

    def list_clusters(self, category: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        params: list[Any] = []
        clause = "status = 'active'"
        if category:
            clause += " AND category = ?"
            params.append(category)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM topic_clusters WHERE {clause} ORDER BY hot_score DESC LIMIT ?",
                params,
            ).fetchall()
        return [_cluster_row(row) for row in rows]

    def save_turn(
        self,
        conversation_id: str,
        user_message: str,
        assistant_answer: str,
        recommendations: list[dict[str, Any]],
        focus_object: dict[str, Any] | None,
        *,
        user_id: str = "default",
        payload: dict[str, Any] | None = None,
        response: dict[str, Any] | None = None,
        topic: str | None = None,
        category_scope: list[str] | None = None,
    ) -> str:
        turn_id = stable_id("turn", f"{conversation_id}:{user_message}:{_now()}")
        now = _now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_turns(
                    id, conversation_id, user_id, user_message, assistant_answer,
                    recommendations_json, focus_object_json, payload_json, response_json, topic,
                    category_scope_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    conversation_id,
                    user_id,
                    user_message,
                    assistant_answer,
                    json.dumps(recommendations, ensure_ascii=False, default=str),
                    json.dumps(focus_object, ensure_ascii=False, default=str) if focus_object else None,
                    json.dumps(payload or {}, ensure_ascii=False, default=str),
                    json.dumps(response, ensure_ascii=False, default=str) if response else None,
                    topic,
                    json.dumps(category_scope or [], ensure_ascii=False),
                    now,
                ),
            )
            conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        return turn_id

    def update_turn_response(self, turn_id: str, user_id: str, response: dict[str, Any]) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE conversation_turns SET response_json = ? WHERE id = ? AND user_id = ?",
                (json.dumps(response, ensure_ascii=False, default=str), turn_id, user_id),
            )
            row = conn.execute("SELECT * FROM conversation_turns WHERE id = ? AND user_id = ?", (turn_id, user_id)).fetchone()
        return _conversation_turn_row(row) if row else None

    def set_turn_relation(self, turn_id: str, user_id: str, relation: str, notice: str) -> dict[str, Any] | None:
        if relation not in {"related", "unrelated"}:
            raise ValueError("relation must be related or unrelated")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM conversation_turns WHERE id = ? AND user_id = ?", (turn_id, user_id)).fetchone()
            if not row:
                return None
            turn = _conversation_turn_row(row)
            response = dict(turn.get("response") or {})
            if not response:
                response = {
                    "conversation_id": turn["conversation_id"],
                    "turn_id": turn_id,
                    "answer": turn.get("assistant_answer") or "",
                    "markdown": turn.get("assistant_answer") or "",
                    "context_relation": "restored_history",
                }
            response["turn_id"] = turn_id
            response["forced_relation"] = relation
            response["context_relation"] = f"forced_{relation}"
            for key in ("answer", "markdown"):
                response[key] = _apply_relation_notice(response.get(key) or "", relation, notice)
            conn.execute(
                "UPDATE conversation_turns SET assistant_answer = ?, response_json = ? WHERE id = ? AND user_id = ?",
                (
                    response.get("answer") or "",
                    json.dumps(response, ensure_ascii=False, default=str),
                    turn_id,
                    user_id,
                ),
            )
            updated = conn.execute("SELECT * FROM conversation_turns WHERE id = ? AND user_id = ?", (turn_id, user_id)).fetchone()
        return _conversation_turn_row(updated) if updated else None

    def last_turn(self, conversation_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        clause = "conversation_id = ?"
        params: list[Any] = [conversation_id]
        if user_id is not None:
            clause += " AND user_id = ?"
            params.append(user_id)
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT * FROM conversation_turns WHERE {clause} ORDER BY created_at DESC, rowid DESC LIMIT 1",
                params,
            ).fetchone()
            if not row and user_id and user_id != "default":
                row = conn.execute(
                    """
                    SELECT * FROM conversation_turns
                    WHERE conversation_id = ? AND user_id = 'default'
                    ORDER BY created_at DESC, rowid DESC LIMIT 1
                    """,
                    (conversation_id,),
                ).fetchone()
        if not row:
            return None
        return _conversation_turn_row(row)

    def list_turns(self, conversation_id: str, user_id: str, limit: int = 40) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM conversation_turns
                WHERE conversation_id = ? AND user_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (conversation_id, user_id, limit),
            ).fetchall()
            if not rows and user_id != "default":
                rows = conn.execute(
                    """
                    SELECT * FROM conversation_turns
                    WHERE conversation_id = ? AND user_id = 'default'
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (conversation_id, limit),
                ).fetchall()
        return [_conversation_turn_row(row) for row in reversed(rows)]

    def list_recent_turns(
        self,
        user_id: str,
        limit: int = 20,
        exclude_conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clause = "user_id = ?"
        params: list[Any] = [user_id]
        if exclude_conversation_id:
            clause += " AND conversation_id != ?"
            params.append(exclude_conversation_id)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM conversation_turns
                WHERE {clause}
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            if not rows and user_id != "default":
                fallback_clause = "user_id = 'default'"
                fallback_params: list[Any] = []
                if exclude_conversation_id:
                    fallback_clause += " AND conversation_id != ?"
                    fallback_params.append(exclude_conversation_id)
                fallback_params.append(limit)
                rows = conn.execute(
                    f"""
                    SELECT * FROM conversation_turns
                    WHERE {fallback_clause}
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    fallback_params,
                ).fetchall()
        return [_conversation_turn_row(row) for row in reversed(rows)]

    def list_conversations(self, user_id: str, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT
                        conversation_id,
                        user_message,
                        assistant_answer,
                        topic,
                        category_scope_json,
                        created_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY conversation_id
                            ORDER BY created_at ASC, rowid ASC
                        ) AS first_rank,
                        ROW_NUMBER() OVER (
                            PARTITION BY conversation_id
                            ORDER BY created_at DESC, rowid DESC
                        ) AS last_rank,
                        COUNT(*) OVER (PARTITION BY conversation_id) AS turn_count
                    FROM conversation_turns
                    WHERE user_id = ?
                )
                SELECT
                    ranked.conversation_id,
                    COALESCE(c.kind, 'chat') AS kind,
                    COALESCE(c.title, ranked.conversation_id) AS title,
                    MAX(CASE WHEN first_rank = 1 THEN user_message END) AS first_message,
                    MAX(CASE WHEN last_rank = 1 THEN user_message END) AS last_message,
                    MAX(CASE WHEN last_rank = 1 THEN assistant_answer END) AS last_answer,
                    MAX(CASE WHEN last_rank = 1 THEN topic END) AS topic,
                    MAX(CASE WHEN last_rank = 1 THEN category_scope_json END) AS category_scope_json,
                    MAX(ranked.created_at) AS updated_at,
                    MAX(turn_count) AS turn_count
                FROM ranked
                LEFT JOIN conversations c ON c.id = ranked.conversation_id AND c.user_id = ?
                GROUP BY ranked.conversation_id, c.kind, c.title
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (user_id, user_id, limit),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["category_scope"] = json.loads(item.pop("category_scope_json", None) or "[]")
            items.append(item)
        return items

    def get_or_create_conversation(self, user_id: str, kind: str = "chat", title: str = "对话") -> dict[str, Any]:
        now = _now()
        conversation_id = stable_id("conv", f"{user_id}:{kind}")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations(id, user_id, title, kind, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, kind) DO UPDATE SET
                  title=excluded.title,
                  updated_at=conversations.updated_at
                """,
                (conversation_id, user_id, title, kind, now, now),
            )
            row = conn.execute("SELECT * FROM conversations WHERE user_id = ? AND kind = ?", (user_id, kind)).fetchone()
        return _conversation_row(row)

    def save_report(self, user_id: str, topic: str, category_scope: list[str], report: dict[str, Any]) -> str:
        report_id = stable_id("rpt", f"{user_id}:{topic}:{_now()}")
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO reports(id, user_id, topic, category_scope_json, report_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (report_id, user_id, topic, json.dumps(category_scope, ensure_ascii=False), json.dumps(report, ensure_ascii=False, default=str), _now()),
            )
        return report_id

    def get_report(self, report_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        clause = "id = ?"
        params: list[Any] = [report_id]
        if user_id is not None:
            clause += " AND user_id = ?"
            params.append(user_id)
        with self.connect() as conn:
            row = conn.execute(f"SELECT * FROM reports WHERE {clause}", params).fetchone()
        if not row:
            return None
        data = dict(row)
        report = json.loads(data.pop("report_json") or "{}")
        if isinstance(report, dict):
            report.setdefault("report_id", data["id"])
            report.setdefault("topic", data.get("topic") or "")
            report.setdefault("category_scope", json.loads(data.get("category_scope_json") or "[]"))
            report.setdefault("created_at", data.get("created_at"))
            return report
        return {
            "report_id": data["id"],
            "topic": data.get("topic") or "",
            "category_scope": json.loads(data.get("category_scope_json") or "[]"),
            "created_at": data.get("created_at"),
            "sections": {},
            "timeline": [],
            "sources": [],
        }

    def create_task(self, task: dict[str, Any]) -> dict[str, Any]:
        task_id = stable_id("task", f"{task.get('user_id')}:{task.get('task_type')}:{_now()}")
        now = _now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_tasks(
                  id, user_id, task_type, schedule_cron, topics_json, category_scope_json,
                  source_scope_json, output_style, raw_task_description, parsed_workflow_json,
                  delivery_channel, enabled, next_run_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    task.get("user_id", "default"),
                    task["task_type"],
                    task["schedule"],
                    json.dumps(task.get("topics", []), ensure_ascii=False),
                    json.dumps(task.get("category_scope", []), ensure_ascii=False),
                    json.dumps(task.get("source_scope", []), ensure_ascii=False),
                    task.get("output_style"),
                    task.get("raw_task_description"),
                    json.dumps(task.get("parsed_workflow") or {}, ensure_ascii=False, default=str),
                    task.get("delivery_channel", "in_app"),
                    1,
                    task.get("next_run_at"),
                    now,
                ),
            )
        return {**task, "id": task_id, "enabled": True, "created_at": now}

    def list_tasks(self, user_id: str | None = None, enabled_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if enabled_only:
            clauses.append("enabled = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM scheduled_tasks
                {where}
                ORDER BY COALESCE(next_run_at, created_at) ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_task_row(row) for row in rows]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
        return _task_row(row) if row else None

    def mark_task_run(self, task_id: str, next_run_at: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE scheduled_tasks SET last_run_at = ?, next_run_at = ? WHERE id = ?",
                (_now(), next_run_at, task_id),
            )

    def set_task_enabled(self, task_id: str, user_id: str, enabled: bool) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE scheduled_tasks SET enabled = ? WHERE id = ? AND user_id = ?",
                (1 if enabled else 0, task_id, user_id),
            )
            row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ? AND user_id = ?", (task_id, user_id)).fetchone()
        return _task_row(row) if row else None

    def delete_task(self, task_id: str, user_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ? AND user_id = ?", (task_id, user_id)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE scheduled_tasks SET enabled = 0 WHERE id = ? AND user_id = ?", (task_id, user_id))
            conn.execute("DELETE FROM scheduled_tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
        data = _task_row(row)
        data["enabled"] = False
        return data

    def create_notification(
        self,
        user_id: str,
        title: str,
        body: str,
        target_type: str | None = None,
        target_id: str | None = None,
        delivery_channel: str = "in_app",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        notification_id = stable_id("ntf", f"{user_id}:{title}:{target_id}:{now}")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO notifications(id, user_id, title, body, target_type, target_id, delivery_channel, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification_id,
                    user_id,
                    title,
                    body,
                    target_type,
                    target_id,
                    delivery_channel,
                    json.dumps(payload or {}, ensure_ascii=False, default=str),
                    now,
                ),
            )
        return {
            "id": notification_id,
            "user_id": user_id,
            "title": title,
            "body": body,
            "target_type": target_type,
            "target_id": target_id,
            "delivery_channel": delivery_channel,
            "payload": payload or {},
            "read_at": None,
            "created_at": now,
        }

    def list_notifications(self, user_id: str, unread_only: bool = False, limit: int = 20) -> list[dict[str, Any]]:
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if unread_only:
            clauses.append("read_at IS NULL")
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM notifications
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_notification_row(row) for row in rows]

    def mark_notification_read(self, notification_id: str, user_id: str) -> dict[str, Any] | None:
        now = _now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE notifications SET read_at = ? WHERE id = ? AND user_id = ?",
                (now, notification_id, user_id),
            )
            row = conn.execute(
                "SELECT * FROM notifications WHERE id = ? AND user_id = ?",
                (notification_id, user_id),
            ).fetchone()
        return _notification_row(row) if row else None

    def upsert_topic(self, topic: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        conversation_id = topic.get("conversation_id") or ""
        topic_id = topic.get("id") or stable_id(
            "topic",
            f"{topic.get('user_id')}:{conversation_id}:{topic.get('topic_type', 'user')}:{topic['title']}",
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pna_topics(
                  id, user_id, conversation_id, title, topic_type, category_scope_json, source_scope_json,
                  watch_keywords_json, refresh_schedule, task_id, status, last_refresh_at,
                  created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, conversation_id, title, topic_type) DO UPDATE SET
                  category_scope_json=excluded.category_scope_json,
                  source_scope_json=excluded.source_scope_json,
                  watch_keywords_json=excluded.watch_keywords_json,
                  refresh_schedule=excluded.refresh_schedule,
                  task_id=COALESCE(excluded.task_id, pna_topics.task_id),
                  status=excluded.status,
                  last_refresh_at=COALESCE(excluded.last_refresh_at, pna_topics.last_refresh_at),
                  updated_at=excluded.updated_at
                """,
                (
                    topic_id,
                    topic.get("user_id", "default"),
                    conversation_id,
                    topic["title"],
                    topic.get("topic_type", "user"),
                    json.dumps(topic.get("category_scope", []), ensure_ascii=False),
                    json.dumps(topic.get("source_scope", []), ensure_ascii=False),
                    json.dumps(topic.get("watch_keywords", []), ensure_ascii=False),
                    topic.get("refresh_schedule", "*/20 * * * *"),
                    topic.get("task_id"),
                    topic.get("status", "active"),
                    topic.get("last_refresh_at"),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM pna_topics WHERE user_id = ? AND conversation_id = ? AND title = ? AND topic_type = ?",
                (topic.get("user_id", "default"), conversation_id, topic["title"], topic.get("topic_type", "user")),
            ).fetchone()
        return _topic_row(row)

    def update_topic_task(self, topic_id: str, task_id: str | None = None, last_refresh_at: str | None = None) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE pna_topics
                SET task_id = COALESCE(?, task_id), last_refresh_at = COALESCE(?, last_refresh_at), updated_at = ?
                WHERE id = ?
                """,
                (task_id, last_refresh_at, _now(), topic_id),
            )
            row = conn.execute("SELECT * FROM pna_topics WHERE id = ?", (topic_id,)).fetchone()
        return _topic_row(row) if row else None

    def list_topics(
        self,
        user_id: str | None = None,
        topic_type: str | None = None,
        limit: int = 50,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["status = 'active'"]
        params: list[Any] = []
        if user_id and conversation_id is not None:
            clauses.append("((user_id = ? AND conversation_id = ?) OR topic_type = 'system')")
            params.extend([user_id, conversation_id])
        elif user_id:
            clauses.append("(user_id = ? OR topic_type = 'system')")
            params.append(user_id)
        if topic_type:
            clauses.append("topic_type = ?")
            params.append(topic_type)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM pna_topics
                WHERE {' AND '.join(clauses)}
                ORDER BY CASE topic_type WHEN 'user' THEN 0 ELSE 1 END, updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_topic_row(row) for row in rows]

    def log(self, operation: str, status: str, target: str | None = None, detail: dict[str, Any] | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO operation_logs(id, operation, target, status, detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (stable_id("log", f"{operation}:{target}:{status}:{_now()}"), operation, target, status, json.dumps(detail or {}, ensure_ascii=False, default=str), _now()),
            )

    def get_service_state(self, key: str) -> dict[str, Any] | None:
        try:
            with self.connect() as conn:
                row = conn.execute("SELECT value_json FROM service_state WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise
        if not row:
            return None
        try:
            value = json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def set_service_state(self, key: str, value: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO service_state(key, value_json, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at""",
                (key, json.dumps(value, ensure_ascii=False, default=str), _now()),
            )

    def seed_demo_articles(self) -> None:
        now = datetime.now(timezone.utc)
        demo = [
            ("politics", "people_politics", "国际粮食安全议题引发多方关注", "多方围绕粮食出口、航运安全和农作物价格展开讨论，国际组织提示部分进口国库存压力上升。"),
            ("politics", "cctv_news_world", "多国就地区冲突影响交换意见", "围绕地区冲突外溢影响，各方关注能源、粮食、航运和供应链稳定。"),
            ("tech", "ithome", "AI Agent 产品更新带动开发工具新一轮竞争", "OpenAI、创业公司和云厂商近期密集发布 AI Agent 开发工具，围绕上下文管理、工具调用和企业集成展开竞争。"),
            ("tech", "huxiu", "国产芯片公司披露新一代推理方案", "多家芯片公司展示面向大模型推理的新品，市场关注能耗、成本和生态适配。"),
            ("auto", "sina_auto", "新能源汽车价格战进入新阶段", "多家车企调整主力车型价格和权益，消费者观望情绪增加，经销商库存和利润承压。"),
            ("auto", "autohome", "智能驾驶车型密集上市", "新车发布会集中强调城市 NOA、激光雷达和端到端模型，行业进入功能兑现周期。"),
            ("game", "gamersky", "热门国产游戏公布大型版本更新", "新版本增加剧情章节、联机玩法和性能优化，玩家关注后续运营节奏。"),
            ("game", "3dm", "电竞赛事决赛周引发讨论", "两支强队将在周末争夺冠军，转会传闻和版本理解成为赛前焦点。"),
            ("economy", "yicai", "消费市场复苏信号继续增强", "服务消费、汽车和数码品类活动带动客流回升，机构关注持续性。"),
            ("anime", "gamersky_acg", "人气动画新篇章定档", "官方公布新季度播出窗口和主视觉图，粉丝讨论角色线和制作阵容。"),
            ("entertainment", "sina_ent", "热门电影定档暑期档", "片方发布预告和主创阵容，市场关注票房竞争和口碑表现。"),
            ("sports", "sina_sports", "球队完成关键引援补强阵容", "俱乐部宣布签下核心位置球员，新赛季战术变化成为关注点。"),
        ]
        for index, (category, source_id, title, content) in enumerate(demo):
            article_id = stable_id("art", f"{source_id}:{title}")
            if self.get_article(article_id):
                continue
            article = NormalizedArticle(
                id=article_id,
                source_id=source_id,
                section_key=category,
                url=f"https://example.local/{source_id}/{index}",
                title=title,
                summary=summarize(content),
                content=content,
                category=category,
                published_at=now - timedelta(hours=index),
                fetched_at=now,
                source_priority=2,
                keywords=extract_keywords(f"{title} {content}"),
                entities=extract_entities(f"{title} {content}"),
                content_hash=content_hash(content),
            )
            self.save_article(article)


def _source_to_dict(source: SourceConfig) -> dict[str, Any]:
    return {
        "source_id": source.source_id,
        "name": source.name,
        "root_domain": source.root_domain,
        "source_type": source.source_type,
        "priority": source.priority,
        "crawl_enabled": source.crawl_enabled,
        "search_enabled": source.search_enabled,
        "categories": list(source.categories),
        "tags": list(source.tags),
        "region": source.region,
        "language": source.language,
        "credibility": source.credibility,
        "crawl_interval_minutes": source.crawl_interval_minutes,
        "sections": [section.__dict__ for section in source.sections],
        "search": {
            "strategy": source.search.strategy,
            "domain_filters": list(source.search.domain_filters),
            "native_search_enabled": source.search.native_search_enabled,
            "candidate_templates": list(source.search.candidate_templates),
        },
        "rate_limit": {
            "min_interval_seconds": source.rate_limit.min_interval_seconds,
            "max_pages_per_run": source.rate_limit.max_pages_per_run,
        },
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _cluster_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["keywords"] = json.loads(data.pop("keywords_json") or "[]")
    data["entities"] = json.loads(data.pop("entities_json") or "[]")
    data["article_ids"] = json.loads(data.pop("article_ids_json") or "[]")
    return data


def _conversation_row(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _conversation_turn_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["recommendations"] = json.loads(data.pop("recommendations_json", None) or "[]")
    data["focus_object"] = json.loads(data.pop("focus_object_json", None) or "null")
    data["payload"] = json.loads(data.pop("payload_json", None) or "{}")
    data["response"] = json.loads(data.pop("response_json", None) or "null")
    data["category_scope"] = json.loads(data.pop("category_scope_json", None) or "[]")
    if not data["response"]:
        data["response"] = {
            "conversation_id": data["conversation_id"],
            "turn_id": data["id"],
            "answer": data["assistant_answer"],
            "markdown": data["assistant_answer"],
            "context_relation": "restored_history",
            "focus_object": data["focus_object"],
            "recommendations": data["recommendations"],
            "research_trace": [],
            "evidence": [],
            "expanded_queries": [],
            "event_line": None,
        }
    elif isinstance(data["response"], dict):
        data["response"]["turn_id"] = data["response"].get("turn_id") or data["id"]
    return data


def _apply_relation_notice(text: str, relation: str, notice: str) -> str:
    cleaned = _remove_relation_notice(text, notice).lstrip()
    if relation == "unrelated":
        return f"> {notice}\n\n{cleaned}" if cleaned else f"> {notice}"
    return cleaned


def _remove_relation_notice(text: str, notice: str) -> str:
    value = str(text or "")
    patterns = (
        f"> {notice}\n\n",
        f"> {notice}\n",
        f"{notice}\n\n",
        notice,
    )
    for pattern in patterns:
        value = value.replace(pattern, "")
    return value


def _task_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["topics"] = json.loads(data.pop("topics_json") or "[]")
    data["category_scope"] = json.loads(data.pop("category_scope_json") or "[]")
    data["source_scope"] = json.loads(data.pop("source_scope_json") or "[]")
    data["raw_task_description"] = data.get("raw_task_description") or ""
    data["parsed_workflow"] = json.loads(data.pop("parsed_workflow_json", None) or "{}")
    data["enabled"] = bool(data["enabled"])
    return data


def _topic_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["category_scope"] = json.loads(data.pop("category_scope_json") or "[]")
    data["source_scope"] = json.loads(data.pop("source_scope_json") or "[]")
    data["watch_keywords"] = json.loads(data.pop("watch_keywords_json") or "[]")
    data["enabled"] = data.get("status") == "active"
    return data


def _notification_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json") or "{}")
    return data
