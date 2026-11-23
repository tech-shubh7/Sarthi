-- Setup sessions: each lives 15 minutes at most; a bot token is kept only between Telegram creating the bot
-- and the user's Sarthi collecting it (seconds), then erased.
CREATE TABLE IF NOT EXISTS sessions (
  id            TEXT PRIMARY KEY,          -- random, given to the Sarthi that asked
  secret_hash   TEXT NOT NULL,             -- sha256 of the secret only that Sarthi knows
  code          TEXT NOT NULL UNIQUE,      -- in the /start link the user opens
  status        TEXT NOT NULL,             -- pending -> paired -> ready -> claimed
  tg_user_id    INTEGER,                   -- the Telegram user who opened the link
  bot_id        INTEGER,
  bot_username  TEXT,
  token         TEXT,                      -- the user's new bot token, until collected
  created_at    INTEGER NOT NULL,
  expires_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (tg_user_id, status);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions (expires_at);

-- Setups started per address in the last hour (a simple brake on abuse).
CREATE TABLE IF NOT EXISTS hits (
  ip  TEXT NOT NULL,
  at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS hits_ip ON hits (ip, at);
