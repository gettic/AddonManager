-- Game data from All-in-One Guide (Cloudflare D1 database "guide-data", binding DATA; see
-- SETUP.md "Game data"). PRIVATE: raw pages are never published; only merged facts go into the
-- guide packs. No IP address, no name, no time finer than a day. Rows older than 183 days are
-- deleted by the Worker's cron (worker.js cleanUp).
CREATE TABLE IF NOT EXISTS gather_pages (
  rid   TEXT PRIMARY KEY,          -- the page's own random id from the addon (dedupe key)
  sid   TEXT NOT NULL,             -- the random sender code made by the Addon Manager
  page  TEXT NOT NULL,             -- the checked "AOGD1 ..." page, as sent
  day   TEXT NOT NULL              -- UTC date it came in, e.g. 2026-11-26
);
CREATE INDEX IF NOT EXISTS gather_pages_sid_day ON gather_pages (sid, day);
CREATE INDEX IF NOT EXISTS gather_pages_day ON gather_pages (day);

-- Sender codes that kept sending bad data (added by hand; their pages are answered "bad").
CREATE TABLE IF NOT EXISTS blocked_sids (
  sid   TEXT PRIMARY KEY,
  day   TEXT NOT NULL              -- UTC date it was blocked
);
