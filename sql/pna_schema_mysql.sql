-- Personal News Agent user/profile tables for the stock_agent database.
-- Run this against the configured stock_agent database connection.

CREATE TABLE IF NOT EXISTS pna_users (
  id VARCHAR(64) PRIMARY KEY,
  username VARCHAR(80) UNIQUE,
  display_name VARCHAR(80) NOT NULL,
  email VARCHAR(255) UNIQUE,
  mobile VARCHAR(20) UNIQUE,
  real_name VARCHAR(40),
  id_card_hash VARCHAR(128),
  id_card_masked VARCHAR(32),
  realname_verified TINYINT DEFAULT 0,
  realname_provider VARCHAR(32),
  realname_request_id VARCHAR(128),
  realname_verified_at DATETIME,
  password_hash TEXT,
  assistant_prompt TEXT,
  created_at DATETIME,
  updated_at DATETIME
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pna_user_profiles (
  user_id VARCHAR(64) PRIMARY KEY,
  self_description TEXT,
  age INT NULL,
  gender VARCHAR(16),
  zodiac VARCHAR(16),
  preferred_categories_json TEXT,
  watch_keywords_json TEXT,
  negative_keywords_json TEXT,
  model_key VARCHAR(80),
  output_style VARCHAR(40) DEFAULT 'concise',
  onboarding_completed TINYINT DEFAULT 0,
  updated_at DATETIME
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pna_auth_identities (
  id VARCHAR(64) PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  provider VARCHAR(32) NOT NULL,
  provider_user_id VARCHAR(128) NOT NULL,
  union_id VARCHAR(128),
  raw_json TEXT,
  created_at DATETIME,
  updated_at DATETIME,
  UNIQUE KEY uq_pna_auth_provider_user (provider, provider_user_id),
  KEY idx_pna_auth_user_id (user_id)
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pna_auth_sessions (
  token VARCHAR(128) PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  created_at DATETIME,
  expires_at DATETIME,
  KEY idx_pna_auth_sessions_user_id (user_id)
) DEFAULT CHARSET=utf8mb4;

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
