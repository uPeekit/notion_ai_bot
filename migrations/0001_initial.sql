-- 0001 initial schema: events, sessions, executions
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  telegram_user_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  message_id INTEGER,
  kind TEXT NOT NULL,
  raw_input TEXT,
  transcription TEXT,
  llm_model TEXT,
  llm_context TEXT,
  llm_response TEXT,
  interpretation TEXT,
  candidate_scores TEXT,
  validation_result TEXT,
  decision TEXT,
  clarification_state TEXT,
  command TEXT,
  executed INTEGER NOT NULL DEFAULT 0,
  notion_page_id TEXT,
  error TEXT,
  duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
  chat_id INTEGER PRIMARY KEY,
  payload TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY,
  event_id INTEGER REFERENCES events(id),
  chat_id INTEGER NOT NULL,
  reply_message_id INTEGER,
  undo TEXT NOT NULL,
  undone INTEGER NOT NULL DEFAULT 0,
  expires_at TEXT NOT NULL
);
