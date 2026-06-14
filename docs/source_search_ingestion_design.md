# Source Search, Crawl, Storage, and Recall Design

## Goal

Native source search should not stop at returning remote links. The production path is:

1. Search local ES first for low-latency recall.
2. If local recall is weak, query configured native source search paths/APIs.
3. Persist discovered URLs into MySQL `pna_crawl_urls`.
4. Follow result links, fetch article content, normalize, and save into SQLite/MySQL article storage.
5. Index normalized articles into ES.
6. Serve the user from ES/local article storage, with native remote results only as a temporary fallback.

This keeps chat, feed, reports, and long-running topic tracking on internal content instead of repeatedly depending on source-site search pages.

## Storage Responsibilities

### MySQL URL Queue

`pna_crawl_urls` is the operational crawl queue and dedupe ledger.

It owns:

- source section URLs and discovered article URLs
- URL-level dedupe by `url_hash`
- fetch status: `pending`, `ok`, `error`
- retry metadata: `fetch_count`, `error_count`, `last_error`
- refresh scheduling: `fetch_interval_minutes`, `next_fetch_at`
- content update tracking: `content_hash`, `article_id`

Any link found from section crawl, native search, API search, or article follow links should enter this table first.

### Article Store

The article store owns normalized content:

- stable article id
- source id, section key, category
- title, summary, content
- published/fetched time
- entities, keywords, content hash

Current local implementation uses SQLite. The same contract can later move to MySQL if article persistence needs to be centralized.

### ES

ES owns recall and ranking over normalized articles.

It should index only normalized articles, not raw search-result links. Search-result links become ES searchable only after content fetch succeeds.

## Query-Time Order

Default user search order:

1. ES recall, filtered by category/source scope.
2. SQLite/local fallback for dev or ES outage.
3. Native source search if internal recall is insufficient.
4. Broad category fallback only after relevant local/native attempts.
5. External provider fallback if configured.

Native results are marked `origin=native`; indexed results are `origin=elasticsearch`. Product surfaces should prefer indexed content for reports and topic tracking.

## Ingestion-Time Order

Native search ingestion uses:

1. Select sources by `category_scope` or `source_scope`.
2. For each enabled native-search source, query at most a small per-source cap.
3. Deduplicate discovered URLs in memory.
4. Upsert discovered URLs into MySQL URL queue.
5. Fetch up to `fetch_articles` result pages.
6. Save normalized articles.
7. Mark URL fetch status and content hash in MySQL.
8. Index successful articles into ES.
9. Optionally expand one hop through article follow links with `follow_depth=1`.

The per-source cap prevents one high-volume source from filling the whole ingest batch.

## Dedupe and Update Rules

URL dedupe:

- primary key is normalized URL hash in `pna_crawl_urls`
- article storage has a unique URL constraint
- ES document id uses stable article id

Content update:

- after fetch, compute `content_hash`
- if the same URL has a new hash, update article content and reindex ES
- keep `last_seen_at` fresh whenever the URL appears again in section/native search

Search-result dedupe:

- dedupe by URL first
- if URL differs but title/content hash is identical, later content-level dedupe can merge or down-rank duplicates

## Refresh Policy

Section URLs:

- recurring crawl every 10-20 minutes in MVP
- source-specific intervals can still be bounded into that range

Article URLs:

- newly discovered articles are fetched quickly
- fetched article URLs should still be eligible for refetch on their interval to detect updates
- repeated errors stay in queue with backoff metadata

Native search:

- triggered on demand for user query, deep-dive expansion, or scheduled topic tracking
- for topic tracking, each generated query variant should run native search ingestion before report synthesis

## Current Implementation

Implemented:

- native HTML search templates for CCTV Sports and Hupu
- native JSON API request mapping for People
- search redirect unwrapping such as CCTV `targetpage`
- query encoding via `{query_encoded}`
- ES-first search with conservative relevance filtering
- native search fallback when ES/local recall is insufficient
- `NativeSearchIngestionService`
- CLI: `scripts/ingest_native_search.py`
- API: `POST /api/news/search/ingest`
- audit CLI: `scripts/audit_source_search.py`

Verified with `张雪机车`:

- native source audit: CCTV Sports and Hupu are `native_html_ready`
- native ingest discovered 10 URLs across CCTV Sports and Hupu
- fetched 8 articles
- indexed 8 articles into ES
- ES now recalls the fetched CCTV/Hupu articles for `张雪机车`

Held:

- Sina Sports, Sohu Sports, and Tencent Sports currently return accessible search pages but no stable article links through static HTML parsing. They are classified as `hold_unparsed_or_js_shell`.

## Next Design Work

The remaining design work before production quality:

1. Move normalized article storage from SQLite-only to MySQL-backed article tables if multi-process/runtime persistence is required.
2. Add content-level duplicate detection beyond URL uniqueness.
3. Add freshness scoring to search ranking: ES score plus source credibility plus recency plus user profile match.
4. Add a background worker that drains pending article URLs from MySQL, instead of only fetching inline during user-triggered ingest.
5. Tie deep-dive query expansion to native ingest, so expanded search terms create durable internal evidence before report synthesis.
6. Add per-source fetch budgets, rate limits, and circuit breakers based on recent error rate.
