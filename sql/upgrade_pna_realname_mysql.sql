ALTER TABLE pna_users ADD COLUMN username VARCHAR(80) UNIQUE;
ALTER TABLE pna_users ADD COLUMN mobile VARCHAR(20) UNIQUE;
ALTER TABLE pna_users ADD COLUMN real_name VARCHAR(40);
ALTER TABLE pna_users ADD COLUMN id_card_hash VARCHAR(128);
ALTER TABLE pna_users ADD COLUMN id_card_masked VARCHAR(32);
ALTER TABLE pna_users ADD COLUMN realname_verified TINYINT DEFAULT 0;
ALTER TABLE pna_users ADD COLUMN realname_provider VARCHAR(32);
ALTER TABLE pna_users ADD COLUMN realname_request_id VARCHAR(128);
ALTER TABLE pna_users ADD COLUMN realname_verified_at DATETIME;
