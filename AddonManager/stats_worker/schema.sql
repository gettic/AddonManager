-- Addon Manager usage counts (Cloudflare D1). Only totals are stored:
-- no IP addresses, no IDs, no times finer than a day, no row per person.

-- People per day: every ping (each player sends at most one a day).
-- addon '_total' / version '' / state '' = number of pings that day (for PING_CAP).
CREATE TABLE IF NOT EXISTS daily (
  day     TEXT    NOT NULL,   -- UTC date, e.g. 2026-11-09
  addon   TEXT    NOT NULL,   -- addon folder name, or AddonManager for the app itself
  version TEXT    NOT NULL,   -- installed version, e.g. 0.1.2
  state   TEXT    NOT NULL,   -- latest / outdated / beta
  n       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, addon, version, state)
);

-- People per week: only each player's first ping of the ISO week, so nobody is
-- counted twice in a week (without any ID).
CREATE TABLE IF NOT EXISTS weekly (
  week    TEXT    NOT NULL,   -- ISO week (UTC), e.g. 2026-W46
  addon   TEXT    NOT NULL,
  version TEXT    NOT NULL,
  state   TEXT    NOT NULL,
  n       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (week, addon, version, state)
);
