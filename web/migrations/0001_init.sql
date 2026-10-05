CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  players TEXT NOT NULL DEFAULT '[]',
  key_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;
CREATE TABLE web_sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
) STRICT;
CREATE TABLE login_failures (username TEXT NOT NULL, ip TEXT NOT NULL, at TEXT NOT NULL) STRICT;
CREATE INDEX login_failures_at ON login_failures(at);
CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL) STRICT;
INSERT INTO meta (key, value) VALUES ('ledger_version', 0);
CREATE TABLE uploads (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  sha256 TEXT NOT NULL,
  filename TEXT NOT NULL,
  size INTEGER NOT NULL,
  encoding TEXT NOT NULL CHECK (encoding IN ('gzip', 'identity')),
  uploaded_at TEXT NOT NULL,
  uploaded_by TEXT NOT NULL,
  uploaded_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  players TEXT NOT NULL DEFAULT '[]',
  shots INTEGER,
  first_shot TEXT,
  last_shot TEXT,
  replace_stored INTEGER NOT NULL DEFAULT 0,
  reverted_at TEXT,
  reverted_by TEXT,
  changed_version INTEGER NOT NULL,
  result TEXT,
  result_version INTEGER
) STRICT;
CREATE INDEX uploads_sha ON uploads(sha256);
CREATE INDEX uploads_uploader ON uploads(uploaded_by_id);
CREATE TABLE upload_players (
  name_lc TEXT NOT NULL,
  upload_id INTEGER NOT NULL REFERENCES uploads(id),
  PRIMARY KEY (name_lc, upload_id)
) STRICT;
CREATE TABLE upload_chunks (
  upload_uid TEXT NOT NULL REFERENCES uploads(uid),
  seq INTEGER NOT NULL,
  data BLOB NOT NULL,
  PRIMARY KEY (upload_uid, seq)
) STRICT;
CREATE TABLE analyses (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  based_on_version INTEGER NOT NULL,
  published_at TEXT NOT NULL,
  sessions INTEGER NOT NULL,
  html_gz BLOB
) STRICT;
