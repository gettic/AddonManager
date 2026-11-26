-- Guide reports (Cloudflare D1). One row per report id: so a report is filed once, and the
-- daily cap can be counted. No IP address, no name, no time finer than a day. The report line
-- is kept only while it waits for GitHub (state 'pending'); once filed it is set to NULL.
CREATE TABLE IF NOT EXISTS reports (
  rid    TEXT    PRIMARY KEY,          -- the report's own id from the addon (6 letters/digits)
  day    TEXT    NOT NULL,             -- UTC date it came in, e.g. 2026-11-25
  via    TEXT    NOT NULL,             -- 'app' (Addon Manager) or 'web' (the web page)
  state  TEXT    NOT NULL,             -- 'pending', 'filed' or 'dropped'
  issue  INTEGER,                      -- the GitHub issue number once filed
  line   TEXT,                         -- the report line, only while pending
  tries  INTEGER NOT NULL DEFAULT 0    -- failed tries to file it
);
CREATE INDEX IF NOT EXISTS reports_day ON reports (day);
CREATE INDEX IF NOT EXISTS reports_state ON reports (state);
