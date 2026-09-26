-- Live company checks (demo page -> Worker -> GitHub Actions -> Worker). One row per check.
-- Apply: npx wrangler d1 execute nights-watch-live --remote --file schema.sql
CREATE TABLE IF NOT EXISTS checks (
  id          TEXT PRIMARY KEY,
  host        TEXT NOT NULL,     -- stripe.com: repeat checks within 24 h reuse the last good result
  url         TEXT NOT NULL,
  status      TEXT NOT NULL,     -- running | done | failed
  steps       TEXT,              -- JSON, live progress from the Action
  result      TEXT,              -- JSON, lane B's /api/overview for a real site
  error       TEXT,
  created_at  INTEGER NOT NULL,  -- unix ms
  updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS checks_host ON checks(host, created_at);
CREATE INDEX IF NOT EXISTS checks_created ON checks(created_at);
