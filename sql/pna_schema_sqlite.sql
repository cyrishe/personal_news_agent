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
