CREATE TABLE insights (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL,
  session_label TEXT NOT NULL,
  created_at TEXT NOT NULL,
  model TEXT NOT NULL,
  body TEXT NOT NULL
) STRICT;
CREATE INDEX insights_user ON insights(user_id, created_at);
INSERT INTO meta (key, value) VALUES ('site_instance', 1 + (random() & 9007199254740990));
