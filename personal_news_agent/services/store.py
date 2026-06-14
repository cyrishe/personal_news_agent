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
CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  user_id TEXT,
  topic TEXT NOT NULL,
  category_scope_json TEXT,
  report_json TEXT NOT NULL,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS conversation_turns (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  user_message TEXT NOT NULL,
  assistant_answer TEXT NOT NULL,
  recommendations_json TEXT,
  focus_object_json TEXT,
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
"""


class NewsStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._ensure_news_source_columns(conn)
            self._ensure_pna_user_columns(conn)
            self._ensure_pna_profile_columns(conn)

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

    def save_article(self, article: NormalizedArticle) -> None:
        with self.connect() as conn:
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

    def list_articles(self, category: str | None = None, limit: int = 50, days: int | None = None) -> list[dict[str, Any]]:
        clauses = ["status = 'active'"]
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

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pna_users WHERE username = ?", (username,)).fetchone()
        return _row(row) if row else None

    def get_user_by_mobile(self, mobile: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pna_users WHERE mobile = ?", (mobile,)).fetchone()
        return _row(row) if row else None

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

    def save_turn(self, conversation_id: str, user_message: str, assistant_answer: str, recommendations: list[dict[str, Any]], focus_object: dict[str, Any] | None) -> str:
        turn_id = stable_id("turn", f"{conversation_id}:{user_message}:{_now()}")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_turns(id, conversation_id, user_message, assistant_answer, recommendations_json, focus_object_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    conversation_id,
                    user_message,
                    assistant_answer,
                    json.dumps(recommendations, ensure_ascii=False, default=str),
                    json.dumps(focus_object, ensure_ascii=False, default=str) if focus_object else None,
                    _now(),
                ),
            )
        return turn_id

    def last_turn(self, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_turns WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        data = _row(row)
        data["recommendations"] = json.loads(data.get("recommendations_json") or "[]")
        data["focus_object"] = json.loads(data.get("focus_object_json") or "null")
        return data

    def save_report(self, user_id: str, topic: str, category_scope: list[str], report: dict[str, Any]) -> str:
        report_id = stable_id("rpt", f"{user_id}:{topic}:{_now()}")
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO reports(id, user_id, topic, category_scope_json, report_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (report_id, user_id, topic, json.dumps(category_scope, ensure_ascii=False), json.dumps(report, ensure_ascii=False, default=str), _now()),
            )
        return report_id

    def create_task(self, task: dict[str, Any]) -> dict[str, Any]:
        task_id = stable_id("task", f"{task.get('user_id')}:{task.get('task_type')}:{_now()}")
        now = _now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_tasks(id, user_id, task_type, schedule_cron, topics_json, category_scope_json, source_scope_json, output_style, delivery_channel, enabled, next_run_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def log(self, operation: str, status: str, target: str | None = None, detail: dict[str, Any] | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO operation_logs(id, operation, target, status, detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (stable_id("log", f"{operation}:{target}:{status}:{_now()}"), operation, target, status, json.dumps(detail or {}, ensure_ascii=False, default=str), _now()),
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


def _task_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["topics"] = json.loads(data.pop("topics_json") or "[]")
    data["category_scope"] = json.loads(data.pop("category_scope_json") or "[]")
    data["source_scope"] = json.loads(data.pop("source_scope_json") or "[]")
    data["enabled"] = bool(data["enabled"])
    return data


def _notification_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json") or "{}")
    return data
